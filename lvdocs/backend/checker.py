"""
LVDOCS.AI — document checking engine (Gemini).

Это исходный скрипт проверки пакета документов, доработанный для веба:
  * ключ и модели берутся из переменных окружения, клиент создаётся лениво
    (сервер стартует и без ключа и честно сообщает об этом);
  * несколько файлов одного типа (страницы паспорта) можно объединять в один запрос
    (collect_package(..., merge_pages=True));
  * документы читаются параллельно (collect_package(..., parallel=True));
  * весь прежний функционал (CLI, поиск файлов, .Rmd-отчёт, правила) сохранён.
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from google import genai
from google.genai import types

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")          # чтение документов
AUDIT_MODEL = os.environ.get("GEMINI_AUDIT_MODEL", "gemini-2.5-pro")  # финальный аудит

# VERIFY the current value on pmlp.gov.lv / mfa.gov.lv before relying on it
MIN_MONTHLY_EUR = int(os.environ.get("MIN_MONTHLY_EUR", "0"))   # 0 = не проверять сумму автоматически
PASSPORT_EXTRA_MONTHS = 3           # паспорт должен действовать ещё 3 мес. после обучения/визы

EXT_TO_MIME = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

SEARCH_DIRS = [
    Path.cwd(),
    Path(__file__).resolve().parent,
    Path.home() / "Downloads",
    Path.home() / "Desktop",
    Path.home() / "Documents",
]

_client = None
_client_lock = threading.Lock()
_tl = threading.local()   # per-thread настройки (merge страниц)


def api_key_configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def get_client():
    """Ленивое создание клиента: ключ читается из GEMINI_API_KEY (никогда не хардкодить!)."""
    global _client
    with _client_lock:
        if _client is None:
            _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        return _client


# =====================================================================
#  1. FILE SEARCH AND LOADING
# =====================================================================

def _find_files(names: list[str], file_path: str | None = None) -> list[Path]:
    """Все подходящие файлы (без учёта регистра/формата) в первой директории, где что-то найдено."""
    if file_path:
        p = Path(file_path).expanduser()
        if p.is_file():
            return [p]
        if not p.is_dir():
            raise FileNotFoundError(f"Путь не найден: {p}")
        dirs = [p]
    else:
        dirs = SEARCH_DIRS

    # passport.pdf, Passport.JPG, passport_1.png, passport-scan.docx ...
    pattern = re.compile(
        r"^(?:%s)(?:[\s_\-].*)?$" % "|".join(re.escape(n) for n in names), re.IGNORECASE
    )
    candidates, seen = [], set()
    for d in dirs:
        if not d.is_dir() or d in seen:
            continue
        seen.add(d)
        for f in d.iterdir():
            if f.is_file() and f.suffix.lower() in EXT_TO_MIME and pattern.match(f.stem):
                candidates.append(f)
        if candidates:
            break
    if not candidates:
        raise FileNotFoundError(
            f"Не найден файл '{names[0]}.*'. Искал в: {', '.join(str(d) for d in dirs)}"
        )
    return candidates


def _find_file(names: list[str], file_path: str | None = None) -> Path:
    """Самый свежий подходящий файл (прежнее поведение)."""
    return max(_find_files(names, file_path), key=lambda f: f.stat().st_mtime)


def _docx_parts(path: Path) -> list:
    """DOCX -> text (paragraphs + tables) and embedded images."""
    try:
        from docx import Document
    except ImportError as e:
        raise ImportError("Для DOCX установите: pip install python-docx") from e

    doc = Document(str(path))
    chunks = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            chunks.append(" | ".join(cell.text.strip() for cell in row.cells))
    parts = []
    if chunks:
        parts.append("DOCUMENT TEXT:\n" + "\n".join(chunks))
    img_mimes = {"image/jpeg", "image/png", "image/webp"}
    for rel in doc.part.rels.values():
        if "image" in rel.reltype and rel.target_part.content_type in img_mimes:
            parts.append(types.Part.from_bytes(data=rel.target_part.blob,
                                               mime_type=rel.target_part.content_type))
    if not parts:
        raise ValueError(f"В DOCX нет текста и изображений: {path.name}")
    return parts


def _load_parts(path: Path) -> list:
    ext = path.suffix.lower()
    if ext not in EXT_TO_MIME:
        raise ValueError(f"Неподдерживаемый формат: {ext}")
    if ext == ".docx":
        return _docx_parts(path)
    return [types.Part.from_bytes(data=path.read_bytes(), mime_type=EXT_TO_MIME[ext])]


def _generate(contents, config, retries: int = 3, model: str | None = None):
    """generate_content with retries on transient API errors."""
    model = model or MODEL
    last = None
    for attempt in range(retries):
        try:
            return get_client().models.generate_content(model=model, contents=contents, config=config)
        except Exception as e:  # network errors, 429, 503
            last = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))  # simple linear backoff
    raise RuntimeError(f"Gemini API недоступен: {last}")


def _extract(names: list[str], file_path: str | None, prompt: str) -> dict:
    """Find the file(s), send them + the prompt to Gemini, return the parsed JSON.
    В режиме merge (веб) все файлы одного типа (страницы) уходят одним запросом."""
    files = _find_files(names, file_path)
    if getattr(_tl, "merge", False):
        files = sorted(files, key=lambda f: f.name)
    else:
        files = [max(files, key=lambda f: f.stat().st_mtime)]
    parts = [part for f in files for part in _load_parts(f)]
    response = _generate(
        [*parts, prompt],
        types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    )
    try:
        data = json.loads(response.text)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"ИИ вернул не-JSON для {files[0].name}") from e
    if isinstance(data, list):  # the model sometimes wraps the object in a list
        data = data[0] if data and isinstance(data[0], dict) else {}
    data["_source_file"] = str(files[0])
    data["_source_files"] = [str(f) for f in files]
    return data


# =====================================================================
#  2. COMMON HELPERS
# =====================================================================

def _parse_date(value):
    """Accepts a YYYY-MM-DD string, returns a date or None."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _result(data: dict, errors: list, warnings: list | None = None) -> dict:
    return {"data": data, "errors": errors, "warnings": warnings or [], "valid": not errors}


def _require(data, fields, errors):
    for f in fields:
        if data.get(f) in (None, "", []):
            errors.append(f"Не найдено поле: {f}")


def _months_ago(d: date, days: int) -> bool:
    """True if date d is older than `days` days from today."""
    return d < date.today() - timedelta(days=days)


COMMON_RULES = (
    "Return ONLY a JSON object. All dates in YYYY-MM-DD format. "
    "If a field is missing or unreadable, use null. Do not guess values. "
    "Transliterate names exactly as printed in the Latin/MRZ part when available."
)

META_FIELDS = (
    "document_language (main language of the document, in English, e.g. 'English', 'Russian'), "
    "has_apostille (true/false: is an Apostille stamp/certificate visible), apostille_country, "
    "has_embassy_legalization (true/false: legalization stamps of a foreign ministry or the "
    "Latvian embassy visible), has_certified_translation (true/false: a sworn/notarized translation "
    "is attached or present in the file), translation_language, is_readable (true/false), "
    "pages_seen (integer number of pages/images seen)"
)


def _prompt(title: str, fields: str, extra: str = "") -> str:
    return (f"Extract data from: {title}. {COMMON_RULES} {extra}\n"
            f"Fields: {fields},\n{META_FIELDS}.")


def _meta_checks(data, errors, warnings):
    if data.get("is_readable") is False:
        errors.append("Документ плохо читается")


# =====================================================================
#  3. DOCUMENT READERS
# =====================================================================

def passport_analyze(file_path: str | None = None) -> dict:
    prompt = _prompt(
        "a PASSPORT (identity page; if other pages are included, also count them)",
        "surname, given_names, nationality (country name in English), document_number, "
        "date_of_birth, sex, place_of_birth, date_of_issue, date_of_expiry, issuing_authority, "
        "mrz_line1, mrz_line2, blank_pages_count (integer: number of completely blank visa pages "
        "visible; null if only the identity page is provided)")
    data = _extract(["passport", "паспорт"], file_path, prompt)
    errors, warnings = [], []
    _require(data, ("surname", "given_names", "nationality", "document_number",
                    "date_of_birth", "date_of_expiry"), errors)
    expiry = _parse_date(data.get("date_of_expiry"))
    if expiry:
        if expiry < date.today():
            errors.append("Паспорт просрочен")
        elif expiry < date.today() + timedelta(days=183):
            warnings.append("Срок действия паспорта меньше 6 месяцев (проверьте +3 мес. после обучения)")
    blanks = data.get("blank_pages_count")
    if blanks is None:
        warnings.append("Не удалось проверить 2 пустые страницы — загрузите скан всех страниц паспорта")
    elif blanks < 2:
        errors.append(f"Пустых страниц меньше 2 (найдено: {blanks})")
    _meta_checks(data, errors, warnings)
    return _result(data, errors, warnings)


def certificate_analyze(file_path: str | None = None) -> dict:
    prompt = _prompt(
        "a SCHOOL LEAVING CERTIFICATE (attestat) and its grades appendix if present",
        "surname, given_names, patronymic, date_of_birth, school_name, document_number, series, "
        "date_of_issue, graduation_year, subjects (list of objects: name, grade), average_grade, "
        "has_seal (true/false), has_signature (true/false)")
    data = _extract(["certificate", "attestat", "аттестат"], file_path, prompt)
    errors, warnings = [], []
    _require(data, ("surname", "given_names", "school_name", "date_of_issue"), errors)
    if not data.get("subjects"):
        errors.append("Не найден список предметов и оценок (приложение)")
    if data.get("has_seal") is False:
        warnings.append("Не обнаружена печать")
    if data.get("has_signature") is False:
        warnings.append("Не обнаружена подпись")
    issued = _parse_date(data.get("date_of_issue"))
    if issued and issued > date.today():
        errors.append("Дата выдачи в будущем")
    _meta_checks(data, errors, warnings)
    return _result(data, errors, warnings)


def diploma_analyze(file_path: str | None = None) -> dict:
    prompt = _prompt(
        "an academic DIPLOMA / DEGREE CERTIFICATE",
        "holder_full_name, date_of_birth, institution_name, institution_country, degree_level "
        "(e.g. secondary/bachelor/master), qualification_title, field_of_study, document_number, "
        "date_of_issue, graduation_year, has_seal (true/false), has_signature (true/false)")
    data = _extract(["diploma", "degree", "диплом"], file_path, prompt)
    errors, warnings = [], []
    _require(data, ("holder_full_name", "institution_name", "degree_level", "date_of_issue"), errors)
    issued = _parse_date(data.get("date_of_issue"))
    if issued and issued > date.today():
        errors.append("Дата выдачи в будущем")
    if data.get("has_seal") is False:
        warnings.append("Не обнаружена печать")
    if data.get("has_signature") is False:
        warnings.append("Не обнаружена подпись")
    _meta_checks(data, errors, warnings)
    return _result(data, errors, warnings)


def transcript_analyze(file_path: str | None = None) -> dict:
    prompt = _prompt(
        "an academic TRANSCRIPT / diploma supplement / grades record",
        "holder_full_name, institution_name, institution_country, program_name, "
        "subjects (list of objects: name, grade, credits), total_credits, gpa_or_average, "
        "date_of_issue, has_seal (true/false), has_signature (true/false)")
    data = _extract(["transcript", "transcripts", "supplement", "приложение"], file_path, prompt)
    errors, warnings = [], []
    _require(data, ("holder_full_name", "institution_name", "subjects", "date_of_issue"), errors)
    if data.get("has_seal") is False:
        warnings.append("Не обнаружена печать")
    if data.get("has_signature") is False:
        warnings.append("Не обнаружена подпись")
    _meta_checks(data, errors, warnings)
    return _result(data, errors, warnings)


def police_clearance_analyze(file_path: str | None = None) -> dict:
    prompt = _prompt(
        "a POLICE CLEARANCE / CRIMINAL RECORD CERTIFICATE",
        "holder_full_name, date_of_birth, passport_number, issuing_authority, issuing_country, "
        "date_of_issue, result (e.g. 'no criminal record'), has_seal (true/false), "
        "has_signature (true/false)")
    data = _extract(["police_clearance", "police", "criminal_record", "справка", "судимост"],
                    file_path, prompt)
    errors, warnings = [], []
    _require(data, ("holder_full_name", "issuing_authority", "date_of_issue"), errors)
    issued = _parse_date(data.get("date_of_issue"))
    if issued:
        if issued > date.today():
            errors.append("Дата выдачи в будущем")
        elif _months_ago(issued, 183):
            errors.append("Справка старше 6 месяцев")
        elif _months_ago(issued, 150):
            warnings.append("Справке почти 6 месяцев — может устареть к моменту подачи")
    result = (data.get("result") or "").lower()
    if result and not any(k in result for k in ("no criminal", "no record", "clear", "нет", "не суд")):
        warnings.append(f"Проверьте вручную содержание справки: {data.get('result')}")
    if data.get("has_seal") is False:
        warnings.append("Не обнаружена печать")
    _meta_checks(data, errors, warnings)
    return _result(data, errors, warnings)


def bank_statement_analyze(file_path: str | None = None) -> dict:
    prompt = _prompt(
        "a BANK STATEMENT / proof of financial means",
        "bank_name, account_holder_name, account_number_or_iban, statement_date (issue date), "
        "period_start, period_end, closing_balance (number), currency, "
        "has_international_card (true/false: Visa/Mastercard card linked to the account is "
        "indicated), card_network, has_bank_stamp_or_signature (true/false)")
    data = _extract(["bank_statement", "bank", "выписка"], file_path, prompt)
    errors, warnings = [], []
    _require(data, ("account_holder_name", "statement_date", "closing_balance", "currency"), errors)
    sd = _parse_date(data.get("statement_date"))
    if sd:
        if sd > date.today():
            errors.append("Дата выписки в будущем")
        elif _months_ago(sd, 30):
            errors.append("Выписка старше 30 дней")
    if data.get("has_international_card") is not True:
        errors.append("Не указана международная карта Visa/Mastercard")
    bal, cur = data.get("closing_balance"), (data.get("currency") or "").upper()
    if isinstance(bal, (int, float)) and MIN_MONTHLY_EUR:
        if cur == "EUR":
            if bal < MIN_MONTHLY_EUR * 12:
                errors.append(f"Баланс {bal} EUR меньше {MIN_MONTHLY_EUR * 12} EUR (12 мес.)")
        else:
            warnings.append(f"Валюта {cur}: пересчитайте в EUR вручную (нужно ≥ {MIN_MONTHLY_EUR * 12} EUR)")
    elif not MIN_MONTHLY_EUR:
        warnings.append("MIN_MONTHLY_EUR не задан — достаточность средств за 12 мес. проверяйте вручную")
    if data.get("has_bank_stamp_or_signature") is False:
        warnings.append("Нет печати/подписи банка")
    _meta_checks(data, errors, warnings)
    return _result(data, errors, warnings)


def pmlp_invitation_analyze(file_path: str | None = None) -> dict:
    prompt = _prompt(
        "a PMLP INVITATION (Izsaukums) / confirmation from a Latvian university",
        "university_name, student_full_name, date_of_birth, passport_number, "
        "invitation_number (Izsaukuma numurs), invitation_date, valid_until, "
        "status (approved/pending/rejected/unknown), program_name, "
        "has_university_seal_or_signature (true/false)")
    data = _extract(["pmlp_invitation", "invitation", "izsaukums", "приглашение"], file_path, prompt)
    errors, warnings = [], []
    _require(data, ("university_name", "student_full_name", "invitation_number"), errors)
    status = (data.get("status") or "unknown").lower()
    if status in ("pending", "rejected"):
        errors.append(f"Статус приглашения: {status}")
    elif status == "unknown":
        warnings.append("Статус приглашения не определён — проверьте активность номера в PMLP")
    vu = _parse_date(data.get("valid_until"))
    if vu and vu < date.today():
        errors.append("Приглашение просрочено")
    if data.get("document_language") and data["document_language"].lower() not in ("latvian", "english"):
        warnings.append("Язык приглашения не Latvian/English")
    _meta_checks(data, errors, warnings)
    return _result(data, errors, warnings)


def university_contract_analyze(file_path: str | None = None) -> dict:
    prompt = _prompt(
        "a UNIVERSITY STUDY AGREEMENT / ENROLLMENT / TUITION CONTRACT",
        "university_name, student_full_name, contract_number, contract_date, program_name, "
        "degree_level, study_start_date, study_end_date, tuition_amount, currency, "
        "payment_schedule, parties (list of names), signed_by_university (true/false), "
        "signed_by_student (true/false), has_university_seal (true/false), "
        "invitation_number_mentioned (string or null)")
    data = _extract(["study_agreement", "university_contract", "agreement", "contract",
                     "контракт", "договор"], file_path, prompt)
    errors, warnings = [], []
    _require(data, ("university_name", "student_full_name", "program_name",
                    "study_start_date", "study_end_date", "tuition_amount"), errors)
    if data.get("signed_by_university") is not True:
        errors.append("Нет подписи университета")
    if data.get("signed_by_student") is not True:
        errors.append("Нет подписи студента")
    if data.get("has_university_seal") is False:
        warnings.append("Не обнаружена печать университета")
    start, end = _parse_date(data.get("study_start_date")), _parse_date(data.get("study_end_date"))
    if start and end and end <= start:
        errors.append("Дата окончания обучения не позже даты начала")
    if end and end < date.today():
        errors.append("Срок обучения по договору уже истёк")
    _meta_checks(data, errors, warnings)
    return _result(data, errors, warnings)


study_agreement_analyze = university_contract_analyze   # alias


def birth_certificate_analyze(file_path: str | None = None) -> dict:
    prompt = _prompt(
        "a BIRTH CERTIFICATE",
        "surname, given_names, patronymic, date_of_birth, place_of_birth, sex, father_full_name, "
        "mother_full_name, registration_date, registry_office, certificate_number, series, "
        "has_seal (true/false)")
    data = _extract(["birth_certificate", "birth", "свидетельство"], file_path, prompt)
    errors, warnings = [], []
    _require(data, ("surname", "given_names", "date_of_birth", "place_of_birth",
                    "certificate_number"), errors)
    if not data.get("mother_full_name") and not data.get("father_full_name"):
        errors.append("Не указаны родители")
    birth, reg = _parse_date(data.get("date_of_birth")), _parse_date(data.get("registration_date"))
    if birth and birth > date.today():
        errors.append("Дата рождения в будущем")
    if birth and reg and reg < birth:
        errors.append("Дата регистрации раньше даты рождения")
    if data.get("has_seal") is False:
        warnings.append("Не обнаружена печать")
    _meta_checks(data, errors, warnings)
    return _result(data, errors, warnings)


def visa_analyze(file_path: str | None = None) -> dict:
    prompt = _prompt(
        "a VISA (sticker or e-visa)",
        "holder_surname, holder_given_names, passport_number, visa_type, visa_number, "
        "issuing_country, issue_date, valid_from, valid_until, number_of_entries, "
        "duration_of_stay_days, purpose")
    data = _extract(["visa", "виза"], file_path, prompt)
    errors, warnings = [], []
    _require(data, ("holder_surname", "passport_number", "visa_type", "valid_from", "valid_until"), errors)
    start, end = _parse_date(data.get("valid_from")), _parse_date(data.get("valid_until"))
    if start and end:
        if end < start:
            errors.append("Дата окончания раньше даты начала")
        if end < date.today():
            errors.append("Виза истекла")
        elif end < date.today() + timedelta(days=30):
            warnings.append("Виза истекает менее чем через 30 дней")
        if start > date.today():
            warnings.append("Виза ещё не вступила в силу")
    _meta_checks(data, errors, warnings)
    return _result(data, errors, warnings)


def health_insurance_analyze(file_path: str | None = None) -> dict:
    prompt = _prompt(
        "a HEALTH INSURANCE form/policy",
        "insurer_name, policyholder_full_name, policy_number, coverage_start, coverage_end, "
        "coverage_amount, currency, coverage_territory (does it cover Latvia / Schengen?), "
        "covers_hospitalization (true/false), covers_emergency (true/false), "
        "covers_repatriation (true/false), deductible, has_stamp_or_signature (true/false)")
    data = _extract(["health_insurance", "insurance", "страховка"], file_path, prompt)
    errors, warnings = [], []
    _require(data, ("insurer_name", "policyholder_full_name", "policy_number",
                    "coverage_start", "coverage_end"), errors)
    start, end = _parse_date(data.get("coverage_start")), _parse_date(data.get("coverage_end"))
    if start and end:
        if end < start:
            errors.append("Дата окончания покрытия раньше даты начала")
        if end < date.today():
            errors.append("Страховка истекла")
        elif end < date.today() + timedelta(days=30):
            warnings.append("Страховка истекает менее чем через 30 дней")
    if data.get("covers_emergency") is False:
        warnings.append("Не покрывает экстренную помощь")
    if data.get("covers_hospitalization") is False:
        warnings.append("Не покрывает госпитализацию")
    if data.get("has_stamp_or_signature") is False:
        warnings.append("Нет печати или подписи")
    _meta_checks(data, errors, warnings)
    return _result(data, errors, warnings)


# =====================================================================
#  4. PACKAGE COLLECTION AND NATIONALITY-BASED RULES
# =====================================================================

# display name -> (is_required, analyzer function)
ANALYZERS = {
    "Passport":           (True,  passport_analyze),
    "Diploma":            (True,  diploma_analyze),
    "Transcript":         (True,  transcript_analyze),
    "Police Clearance":   (True,  police_clearance_analyze),
    "Bank Statement":     (True,  bank_statement_analyze),
    "PMLP Invitation":    (True,  pmlp_invitation_analyze),
    "Study Agreement":    (True,  university_contract_analyze),
    "School Certificate": (False, certificate_analyze),
    "Birth Certificate":  (False, birth_certificate_analyze),
    "Visa":               (False, visa_analyze),
    "Health Insurance":   (False, health_insurance_analyze),
}

NEEDS_APOSTILLE = {"Diploma", "Transcript", "Police Clearance", "School Certificate"}
NO_TRANSLATION_CHECK = {"Passport"}

# WARNING: verify these lists against the current data of the Latvian MFA / HCCH.
_EXEMPT = {
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia", "czech republic", "denmark",
    "estonia", "finland", "france", "germany", "greece", "hungary", "ireland", "italy", "latvia",
    "lithuania", "luxembourg", "malta", "netherlands", "poland", "portugal", "romania", "slovakia",
    "slovenia", "spain", "sweden", "iceland", "liechtenstein", "norway", "united kingdom", "uk",
    "switzerland", "united states", "united states of america", "usa", "canada", "australia",
}
_BILATERAL = {"ukraine", "uzbekistan", "moldova", "belarus"}
_HAGUE = {
    "india", "turkey", "türkiye", "turkiye", "kazakhstan", "georgia", "armenia", "azerbaijan",
    "kyrgyzstan", "pakistan", "philippines", "israel", "japan", "south korea", "china", "mexico",
    "brazil", "morocco", "russia",
}
_NON_HAGUE = {"nigeria", "nepal", "cameroon", "sri lanka"}


def legalization_category(nationality: str | None) -> str:
    """Map nationality to the legalization regime (computed in code, not guessed by the AI)."""
    n = (nationality or "").strip().lower()
    if n in _EXEMPT:
        return "Exempt (EU/EEA/UK/CH/US/CA/AU)"
    if n in _BILATERAL:
        return "Exempt (bilateral agreement) — verify document types"
    if n in _HAGUE:
        return "Hague Apostille Required"
    if n in _NON_HAGUE:
        return "Double Legalization Required"
    return "UNKNOWN — verify country status manually (Hague / bilateral / neither)"


def _run_one(name, required, fn, folder, merge):
    _tl.merge = merge
    try:
        res = fn(folder)
        res["missing"] = False
        return res
    except FileNotFoundError as e:
        if required:
            return {"data": {}, "errors": ["Файл отсутствует"], "warnings": [],
                    "valid": False, "missing": True, "note": str(e)}
        return None  # optional and absent: silently skipped
    except Exception as e:  # API failure, bad JSON, corrupt file, ...
        return {"data": {}, "errors": [f"Ошибка обработки файла: {e}"],
                "warnings": [], "valid": False, "missing": False}


def collect_package(folder: str | None = None, merge_pages: bool = False,
                    parallel: bool = False, workers: int = 4) -> dict:
    """Read all documents. Missing required ones are marked with missing=True;
    missing optional ones are silently skipped."""
    jobs = [(n, req, fn) for n, (req, fn) in ANALYZERS.items()]
    if parallel:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            outs = list(ex.map(lambda j: _run_one(j[0], j[1], j[2], folder, merge_pages), jobs))
    else:
        outs = [_run_one(n, req, fn, folder, merge_pages) for n, req, fn in jobs]
    return {j[0]: o for j, o in zip(jobs, outs) if o is not None}


def apply_rules(results: dict, nationality: str | None = None) -> str:
    """Add language, translation, apostille/legalization and cross-document checks
    to the results (in place). Returns the legalization category."""
    if nationality is None:
        nationality = results.get("Passport", {}).get("data", {}).get("nationality")
    category = legalization_category(nationality)

    for name, res in results.items():
        if res.get("missing") or not res["data"]:
            continue
        d, errs, warns = res["data"], res["errors"], res["warnings"]

        lang = (d.get("document_language") or "").strip().lower()
        if name not in NO_TRANSLATION_CHECK:
            if not lang:
                warns.append("Язык документа не определён")
            elif lang not in ("english", "latvian") and d.get("has_certified_translation") is not True:
                errs.append(f"Язык '{d.get('document_language')}': нужен заверенный перевод на EN/LV")
            elif lang not in ("english", "latvian"):
                warns.append("Перевод есть — убедитесь, что он заверен присяжным переводчиком "
                             "и сшит с оригиналом/копией")

        if name in NEEDS_APOSTILLE:
            if category.startswith("Hague") and d.get("has_apostille") is not True:
                errs.append("Отсутствует апостиль (страна — участник Гаагской конвенции)")
            elif category.startswith("Double"):
                if d.get("has_embassy_legalization") is not True:
                    errs.append("Нет двойной легализации (MFA страны выдачи + посольство/MFA Латвии)")
            elif category.startswith("UNKNOWN") and not (d.get("has_apostille") or d.get("has_embassy_legalization")):
                warns.append("Статус легализации страны неизвестен, апостиль/легализация не найдены")
        res["valid"] = not errs

    results["Cross-document checks"] = _cross_checks(results)
    return category


def _norm(s) -> str:
    return re.sub(r"[^A-Z]", "", (s or "").upper())


def _name_in(text, surname, given) -> bool:
    t = _norm(text)
    first = _norm((given or "").split()[0]) if given else ""
    return bool(t) and _norm(surname) in t and (not first or first in t)


def _cross_checks(results: dict) -> dict:
    """Consistency checks between documents (names, DOB, passport number, university, ...)."""
    errors, warnings = [], []
    g = lambda k: results.get(k, {}).get("data", {}) or {}
    p = g("Passport")
    sur, giv = p.get("surname"), p.get("given_names")

    name_fields = {
        "Diploma": ["holder_full_name"], "Transcript": ["holder_full_name"],
        "Police Clearance": ["holder_full_name"], "Bank Statement": ["account_holder_name"],
        "PMLP Invitation": ["student_full_name"], "Study Agreement": ["student_full_name"],
        "Health Insurance": ["policyholder_full_name"],
        "Visa": ["holder_surname", "holder_given_names"],
    }
    if sur:
        for doc, fields in name_fields.items():
            text = " ".join(str(g(doc).get(f) or "") for f in fields)
            if text.strip() and not _name_in(text, sur, giv):
                warnings.append(f"{doc}: имя не совпадает с паспортом ('{text.strip()}' vs "
                                f"'{sur} {giv}') — проверьте транслитерацию")

    dob = p.get("date_of_birth")
    for doc in ("Diploma", "Police Clearance", "PMLP Invitation", "Birth Certificate"):
        other = g(doc).get("date_of_birth")
        if dob and other and other != dob:
            errors.append(f"{doc}: дата рождения {other} ≠ паспорт {dob}")

    pn = p.get("document_number")
    for doc in ("Police Clearance", "PMLP Invitation", "Visa"):
        other = g(doc).get("passport_number")
        if pn and other and _norm(other) != _norm(pn) and re.sub(r"\W", "", other) != re.sub(r"\W", "", pn):
            errors.append(f"{doc}: номер паспорта {other} ≠ {pn}")

    u1, u2 = g("PMLP Invitation").get("university_name"), g("Study Agreement").get("university_name")
    if u1 and u2 and _norm(u1) != _norm(u2):
        warnings.append(f"Университет в приглашении ('{u1}') и в договоре ('{u2}') различается")
    inv1, inv2 = g("PMLP Invitation").get("invitation_number"), g("Study Agreement").get("invitation_number_mentioned")
    if inv1 and inv2 and _norm(inv1) != _norm(inv2) and str(inv1) != str(inv2):
        errors.append(f"Номер приглашения: {inv1} ≠ {inv2} (в договоре)")

    expiry, end = _parse_date(p.get("date_of_expiry")), _parse_date(g("Study Agreement").get("study_end_date"))
    if expiry and end and expiry < end + timedelta(days=30 * PASSPORT_EXTRA_MONTHS):
        errors.append(f"Паспорт действует до {expiry}, а обучение заканчивается {end}: "
                      f"нужен запас минимум {PASSPORT_EXTRA_MONTHS} мес. после окончания")

    lvl_d = (g("Diploma").get("degree_level") or "").lower()
    lvl_p = (g("Study Agreement").get("degree_level") or "").lower()
    if "master" in lvl_p and lvl_d and "bachelor" not in lvl_d and "master" not in lvl_d:
        errors.append(f"Программа '{lvl_p}' требует степень бакалавра, в дипломе: '{lvl_d}'")
    if "bachelor" in lvl_p and lvl_d and not any(k in lvl_d for k in ("secondary", "school", "bachelor", "master")):
        warnings.append(f"Проверьте, достаточен ли уровень образования '{lvl_d}' для бакалавриата")

    return _result({}, errors, warnings) | {"missing": False}


# =====================================================================
#  5. SECOND AI PASS — FINAL AUDIT
# =====================================================================

AUDIT_SYSTEM_PROMPT = """# System Role
You are an expert Document Verification Specialist for Latvian Student Visa (Type D) and Temporary Residence Permit (TRP / TUA) applications, following the rules of OCMA/PMLP and the Latvian MFA.
You audit a student's document package BEFORE submission to a Latvian Embassy / VFS / PMLP.

# How the input works
You receive JSON with: today's date, applicant nationality, a PRE-COMPUTED legalization category, and for every document: status (present/missing), data extracted by OCR/AI, and automatic errors/warnings already found by code. Optionally the original files are attached too.
- Treat the extracted data as evidence. Never invent facts. If something cannot be confirmed from the data or files, write "UNVERIFIED — check manually".
- Do not contradict the pre-computed legalization category unless the original files clearly prove otherwise; if the category is UNKNOWN, say so and tell the student to check the MFA of Latvia website.
- Do not invent the PMLP minimum monthly amount; use the one given in the input or say it must be verified.
- Use today's date from the input for all validity calculations.

# Mandatory Verification Rules
1. Legalization: EU/EEA/UK/CH/US/CA/AU exempt. Bilateral-agreement countries (e.g. Ukraine, Uzbekistan, Moldova, Belarus): exempt for state-issued official documents (note exceptions to verify). Hague countries: Apostille MANDATORY on diplomas, transcripts, police clearance. Non-Hague countries: DOUBLE legalization (MFA of issuing country + Latvian Embassy/MFA).
2. Language: English or Latvian only; otherwise a certified translation by a sworn translator (Zvērināts tulkotājs) or notarized translation, permanently bound to the original or its certified copy.
3. Academic qualification: diploma level must match the chosen program (bachelor needs secondary education, master needs a bachelor degree); transcripts needed.
4. University: study agreement signed by BOTH parties; PMLP invitation number (Izsaukuma numurs) present, approved and consistent between invitation and agreement.
5. Police clearance: issued within the last 6 months from today (non-EU applicants); legalization + EN/LV translation.
6. Finances: funds for at least 12 months of living expenses; Visa/Mastercard card linked; statement issued within the last 30 days.
7. Passport: valid at least 3 months beyond the end of study/visa; at least 2 blank pages.
8. Consistency: names, date of birth, passport number, university and invitation number must match across documents.

# Output (strictly this structure, in {LANG})
## 1. Executive Summary
* Overall Compliance: APPROVED / ACTION REQUIRED / REJECTED (APPROVED only if every required document is present and PASS with no UNVERIFIED critical items; REJECTED if critical documents are missing or invalid)
* Applicant Nationality / Target Latvian University / Legalization Category
## 2. Document Audit Matrix
| Document Type | File Status | Original Language | Translation Status | Apostille / Legalization | Compliance Status | Required Actions |
Rows: Passport, Diploma/Degree, Transcripts, Police Clearance, Bank Statement, PMLP Invitation, Study Agreement (+ any optional documents provided).
## 3. Mandatory Fixes & Compliance Warnings
* Apostille / Legalization Warnings
* Language & Translation Failures
* Expiration & Validity Issues
* Consistency Problems
* UNVERIFIED items
## 4. Final Submission Checklist
Numbered, actionable steps in priority order, each naming the exact document.
"""


def _clean_data(data: dict) -> dict:
    """Убрать служебные поля (пути к файлам)."""
    return {k: v for k, v in (data or {}).items() if not k.startswith("_source")}


def audit_package(results: dict, nationality: str | None = None, include_files: bool = False,
                  report_language: str = "Russian", model: str | None = None) -> str:
    """Second AI pass: send the whole package to the AI for the final audit.
    Returns a Markdown report. include_files=True also attaches the original files."""
    model = model or AUDIT_MODEL
    category = apply_rules(results, nationality) if "Cross-document checks" not in results \
        else legalization_category(nationality or results.get("Passport", {}).get("data", {}).get("nationality"))
    p = results.get("Passport", {}).get("data", {})
    payload = {
        "today": date.today().isoformat(),
        "applicant_nationality": nationality or p.get("nationality"),
        "legalization_category_precomputed": category,
        "target_university": (results.get("Study Agreement", {}).get("data", {}).get("university_name")
                              or results.get("PMLP Invitation", {}).get("data", {}).get("university_name")),
        "min_monthly_eur_for_check": MIN_MONTHLY_EUR or "not configured — verify on pmlp.gov.lv",
        "documents": {
            name: {
                "status": "MISSING" if r.get("missing") else "PRESENT",
                "automatic_valid": r["valid"],
                "errors": r["errors"],
                "warnings": r["warnings"],
                "extracted_data": _clean_data(r["data"]),
            } for name, r in results.items()
        },
    }
    contents = [json.dumps(payload, ensure_ascii=False, indent=2)]
    if include_files:  # re-send the original scans so the AI can double-check the extraction
        for name, r in results.items():
            srcs = r["data"].get("_source_files") or ([r["data"]["_source_file"]] if r["data"].get("_source_file") else [])
            if srcs:
                contents.append(f"--- ORIGINAL FILE: {name} ---")
                for src in srcs:
                    contents += _load_parts(Path(src))

    response = _generate(
        contents,
        types.GenerateContentConfig(
            system_instruction=AUDIT_SYSTEM_PROMPT.replace("{LANG}", report_language),
            temperature=0,
        ),
        model=model,
    )
    return response.text


# =====================================================================
#  6. R MARKDOWN REPORT
# =====================================================================

def To_rmd(results: dict, output_path: str = "report.Rmd", title: str = "Document check report",
           audit_text: str | None = None) -> str:
    """Save the check results (and the final AI audit, if provided) to an .Rmd file."""
    lines = ["---", f'title: "{title}"', f'date: "{date.today().isoformat()}"',
             "output: html_document", "---", ""]
    if audit_text:
        lines += ["# AI Audit Report", "", audit_text, "", "---", ""]
    lines += ["# Technical details", "", "## Summary", "",
              "| Document | Valid | Errors | Warnings |", "|---|---|---|---|"]
    for name, res in results.items():
        lines.append(f"| {name} | {'yes' if res['valid'] else 'no'} | "
                     f"{len(res['errors'])} | {len(res['warnings'])} |")
    for name, res in results.items():
        lines += ["", f"## {name}", "", f"**Valid:** {'yes' if res['valid'] else 'no'}", ""]
        if res["errors"]:
            lines += ["**Errors:**", ""] + [f"- {e}" for e in res["errors"]] + [""]
        if res["warnings"]:
            lines += ["**Warnings:**", ""] + [f"- {w}" for w in res["warnings"]] + [""]
        if res["data"]:
            lines += ["```json", json.dumps(_clean_data(res["data"]), ensure_ascii=False, indent=2), "```"]
    Path(output_path).write_text("\n".join(lines), encoding="utf-8")
    return output_path


# =====================================================================
#  7. ALL-IN-ONE RUN (CLI)
# =====================================================================

def run_full_check(folder: str | None = None, nationality: str | None = None,
                   output_path: str = "report.Rmd", include_files: bool = False) -> dict:
    results = collect_package(folder)                            # pass 1: read the documents
    apply_rules(results, nationality)                            # code rules
    audit = audit_package(results, nationality, include_files)   # pass 2: AI audit
    To_rmd(results, output_path, audit_text=audit)
    return {"results": results, "audit": audit, "report": output_path}


if __name__ == "__main__":
    out = run_full_check()
    print(out["audit"])
    print("\nReport saved to:", out["report"])
