"""
LVDOCS.AI — web server.
Serves the frontend (../frontend folder) and the API:
    GET  /api/health    — server status and whether a Gemini key is set
    POST /api/analyze   — AI check of the document package from the profile
Run:  python app.py     (default http://127.0.0.1:5000)
"""
import base64
import os
import re
import tempfile
import threading
from pathlib import Path

from flask import Flask, jsonify, request

try:
    from dotenv import load_dotenv
    # override=True: values from .env take priority over system environment variables
    # (otherwise a stale GEMINI_AUDIT_MODEL from Windows/the terminal overrides .env)
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    load_dotenv(override=True)
except ImportError:
    pass

import checker

ROOT = Path(__file__).resolve().parent.parent
app = Flask(__name__, static_folder=str(ROOT / "frontend"), static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024      # whole request is at most 60 MB

MAX_DOCS = 30                       # documents per request
MAX_IMG_BYTES = 8 * 1024 * 1024     # size of a single image
MAX_PARALLEL_RUNS = 2               # concurrent checks (protects the Gemini quota)
_slots = threading.BoundedSemaphore(MAX_PARALLEL_RUNS)

# profile type (check.html) -> file name that checker.py looks for
TYPE_TO_STEM = {
    "passport": "passport",
    "diploma": "diploma",
    "transcript": "transcript",
    "criminal": "police_clearance",
    "bank_statement": "bank_statement",
    "pmlp_invitation": "pmlp_invitation",
    "study_agreement": "study_agreement",
    "school_certificate": "certificate",
    "birth_cert": "birth_certificate",
    "visa_c": "visa",
    "visa_d": "visa",
    "insurance": "health_insurance",
}
LANGS = {"ru": "Russian", "lv": "Latvian", "en": "English"}
DATA_URL = re.compile(r"^data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=\s]+)$")
EXT = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}


# ---------------------------------------------------------------------
#  Translation of automatic-check messages (checker.py writes in Russian)
#  Format: (Russian template, English, Latvian); {name} is the variable part.
#  An unknown message is left as is (Russian); nothing breaks.
# ---------------------------------------------------------------------
_MESSAGES = [
    ("Не найдено поле: {f}", "Field not found: {f}", "Lauks nav atrasts: {f}"),
    ("Документ плохо читается", "The document is hard to read", "Dokuments ir slikti salasāms"),
    ("Паспорт просрочен", "The passport has expired", "Pase ir beigusies"),
    ("Срок действия паспорта меньше 6 месяцев (проверьте +3 мес. после обучения)",
     "Passport validity is under 6 months (check the +3 months after studies requirement)",
     "Pases derīguma termiņš ir mazāks par 6 mēnešiem (pārbaudiet prasību +3 mēneši pēc studijām)"),
    ("Не удалось проверить 2 пустые страницы — загрузите скан всех страниц паспорта",
     "Could not verify the 2 blank pages — upload scans of all passport pages",
     "Nevarēja pārbaudīt 2 tukšās lapas — augšupielādējiet visu pases lappušu skenējumus"),
    ("Пустых страниц меньше 2 (найдено: {n})", "Fewer than 2 blank pages (found: {n})",
     "Mazāk nekā 2 tukšās lapas (atrastas: {n})"),
    ("Не найден список предметов и оценок (приложение)", "List of subjects and grades (appendix) not found",
     "Nav atrasts priekšmetu un atzīmju saraksts (pielikums)"),
    ("Не обнаружена печать", "No seal detected", "Zīmogs nav atrasts"),
    ("Не обнаружена подпись", "No signature detected", "Paraksts nav atrasts"),
    ("Дата выдачи в будущем", "Issue date is in the future", "Izdošanas datums ir nākotnē"),
    ("Справка старше 6 месяцев", "The certificate is older than 6 months", "Izziņa ir vecāka par 6 mēnešiem"),
    ("Справке почти 6 месяцев — может устареть к моменту подачи",
     "The certificate is almost 6 months old — it may expire before submission",
     "Izziņai gandrīz 6 mēneši — tā var novecot līdz iesniegšanai"),
    ("Проверьте вручную содержание справки: {x}", "Check the certificate content manually: {x}",
     "Pārbaudiet izziņas saturu manuāli: {x}"),
    ("Дата выписки в будущем", "Statement date is in the future", "Izraksta datums ir nākotnē"),
    ("Выписка старше 30 дней", "The statement is older than 30 days", "Izraksts ir vecāks par 30 dienām"),
    ("Не указана международная карта Visa/Mastercard", "No linked international Visa/Mastercard card indicated",
     "Nav norādīta starptautiska Visa/Mastercard karte"),
    ("Баланс {bal} EUR меньше {n} EUR (12 мес.)", "Balance {bal} EUR is below {n} EUR (12 months)",
     "Atlikums {bal} EUR ir mazāks par {n} EUR (12 mēneši)"),
    ("Валюта {cur}: пересчитайте в EUR вручную (нужно ≥ {n} EUR)",
     "Currency {cur}: convert to EUR manually (≥ {n} EUR required)",
     "Valūta {cur}: pārrēķiniet EUR manuāli (nepieciešams ≥ {n} EUR)"),
    ("MIN_MONTHLY_EUR не задан — достаточность средств за 12 мес. проверяйте вручную",
     "MIN_MONTHLY_EUR is not set — check sufficiency of funds for 12 months manually",
     "MIN_MONTHLY_EUR nav iestatīts — līdzekļu pietiekamību 12 mēnešiem pārbaudiet manuāli"),
    ("Нет печати/подписи банка", "No bank stamp/signature", "Nav bankas zīmoga/paraksta"),
    ("Статус приглашения: {s}", "Invitation status: {s}", "Izsaukuma statuss: {s}"),
    ("Статус приглашения не определён — проверьте активность номера в PMLP",
     "Invitation status unknown — check that the number is active in PMLP",
     "Izsaukuma statuss nav noteikts — pārbaudiet numura aktivitāti PMLP"),
    ("Приглашение просрочено", "The invitation has expired", "Izsaukums ir beidzies"),
    ("Язык приглашения не Latvian/English", "The invitation is not in Latvian/English",
     "Izsaukums nav latviešu/angļu valodā"),
    ("Нет подписи университета", "No university signature", "Nav universitātes paraksta"),
    ("Нет подписи студента", "No student signature", "Nav studenta paraksta"),
    ("Не обнаружена печать университета", "No university seal detected", "Universitātes zīmogs nav atrasts"),
    ("Дата окончания обучения не позже даты начала", "Study end date is not later than the start date",
     "Studiju beigu datums nav vēlāks par sākuma datumu"),
    ("Срок обучения по договору уже истёк", "The study period in the agreement has already ended",
     "Līgumā noteiktais studiju periods jau ir beidzies"),
    ("Не указаны родители", "Parents are not specified", "Vecāki nav norādīti"),
    ("Дата рождения в будущем", "Date of birth is in the future", "Dzimšanas datums ir nākotnē"),
    ("Дата регистрации раньше даты рождения", "Registration date is earlier than the date of birth",
     "Reģistrācijas datums ir agrāks par dzimšanas datumu"),
    ("Дата окончания раньше даты начала", "End date is earlier than the start date",
     "Beigu datums ir agrāks par sākuma datumu"),
    ("Виза истекла", "The visa has expired", "Vīza ir beigusies"),
    ("Виза истекает менее чем через 30 дней", "The visa expires in less than 30 days",
     "Vīza beigsies mazāk nekā pēc 30 dienām"),
    ("Виза ещё не вступила в силу", "The visa is not yet valid", "Vīza vēl nav spēkā"),
    ("Дата окончания покрытия раньше даты начала", "Coverage end date is earlier than the start date",
     "Seguma beigu datums ir agrāks par sākuma datumu"),
    ("Страховка истекла", "The insurance has expired", "Apdrošināšana ir beigusies"),
    ("Страховка истекает менее чем через 30 дней", "The insurance expires in less than 30 days",
     "Apdrošināšana beigsies mazāk nekā pēc 30 dienām"),
    ("Не покрывает экстренную помощь", "Does not cover emergency care", "Neietver neatliekamo palīdzību"),
    ("Не покрывает госпитализацию", "Does not cover hospitalisation", "Neietver hospitalizāciju"),
    ("Нет печати или подписи", "No stamp or signature", "Nav zīmoga vai paraksta"),
    ("Файл отсутствует", "File is missing", "Fails nav pievienots"),
    ("Ошибка обработки файла: {e}", "File processing error: {e}", "Faila apstrādes kļūda: {e}"),
    ("Язык документа не определён", "Document language not determined", "Dokumenta valoda nav noteikta"),
    ("Язык '{l}': нужен заверенный перевод на EN/LV",
     "Language '{l}': a certified translation into EN/LV is required",
     "Valoda '{l}': nepieciešams apliecināts tulkojums angļu/latviešu valodā"),
    ("Перевод есть — убедитесь, что он заверен присяжным переводчиком и сшит с оригиналом/копией",
     "A translation is present — make sure it is certified by a sworn translator and bound to the original/copy",
     "Tulkojums ir — pārliecinieties, ka to apliecinājis zvērināts tulks un tas ir sašūts ar oriģinālu/kopiju"),
    ("Отсутствует апостиль (страна — участник Гаагской конвенции)",
     "Apostille is missing (the country is a Hague Convention member)",
     "Trūkst apostila (valsts ir Hāgas konvencijas dalībvalsts)"),
    ("Нет двойной легализации (MFA страны выдачи + посольство/MFA Латвии)",
     "Double legalisation is missing (MFA of the issuing country + Latvian embassy/MFA)",
     "Nav dubultās legalizācijas (izdevējvalsts MFA + Latvijas vēstniecība/MFA)"),
    ("Статус легализации страны неизвестен, апостиль/легализация не найдены",
     "The country's legalisation status is unknown and no apostille/legalisation was found",
     "Valsts legalizācijas statuss nav zināms, apostils/legalizācija nav atrasta"),
    ("{doc}: имя не совпадает с паспортом ('{t}' vs '{s}') — проверьте транслитерацию",
     "{doc}: name does not match the passport ('{t}' vs '{s}') — check the transliteration",
     "{doc}: vārds nesakrīt ar pasi ('{t}' vs '{s}') — pārbaudiet transliterāciju"),
    ("{doc}: дата рождения {a} ≠ паспорт {b}", "{doc}: date of birth {a} ≠ passport {b}",
     "{doc}: dzimšanas datums {a} ≠ pase {b}"),
    ("{doc}: номер паспорта {a} ≠ {b}", "{doc}: passport number {a} ≠ {b}", "{doc}: pases numurs {a} ≠ {b}"),
    ("Университет в приглашении ('{a}') и в договоре ('{b}') различается",
     "University differs between the invitation ('{a}') and the agreement ('{b}')",
     "Universitāte izsaukumā ('{a}') un līgumā ('{b}') atšķiras"),
    ("Номер приглашения: {a} ≠ {b} (в договоре)", "Invitation number: {a} ≠ {b} (in the agreement)",
     "Izsaukuma numurs: {a} ≠ {b} (līgumā)"),
    ("Паспорт действует до {e}, а обучение заканчивается {d}: нужен запас минимум {m} мес. после окончания",
     "Passport is valid until {e}, but studies end on {d}: at least {m} months of validity after the end are required",
     "Pase derīga līdz {e}, bet studijas beidzas {d}: nepieciešams vismaz {m} mēnešu derīguma termiņš pēc beigām"),
    ("Программа '{p}' требует степень бакалавра, в дипломе: '{d}'",
     "Program '{p}' requires a bachelor's degree, the diploma says: '{d}'",
     "Programma '{p}' prasa bakalaura grādu, diplomā: '{d}'"),
    ("Проверьте, достаточен ли уровень образования '{l}' для бакалавриата",
     "Check whether the education level '{l}' is sufficient for a bachelor's program",
     "Pārbaudiet, vai izglītības līmenis '{l}' ir pietiekams bakalaura programmai"),
]


def _compile_messages():
    out = []
    for ru, en, lv in _MESSAGES:
        rx = re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>.+?)", re.escape(ru))
        out.append((re.compile(rx, re.S), {"en": en, "lv": lv}))
    return out


_COMPILED = _compile_messages()


def translate_message(msg: str, lang: str) -> str:
    """ru -> en/lv. Unknown messages are returned unchanged."""
    if lang not in ("en", "lv"):
        return msg
    for rx, tpl in _COMPILED:
        m = rx.fullmatch(msg)
        if m:
            return tpl[lang].format(**m.groupdict())
    return msg


def translate_results(results: dict, lang: str) -> dict:
    return {name: {**r, "errors": [translate_message(e, lang) for e in r["errors"]],
                   "warnings": [translate_message(w, lang) for w in r["warnings"]]}
            for name, r in results.items()}


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/health")
def health():
    return jsonify(ok=True, gemini_key=checker.api_key_configured())


@app.post("/api/analyze")
def analyze():
    if not checker.api_key_configured():
        return jsonify(error="no_key"), 503

    body = request.get_json(silent=True) or {}
    docs = body.get("documents")
    if not isinstance(docs, list) or not docs:
        return jsonify(error="no_documents"), 400

    nationality = (body.get("nationality") or "").strip()[:60] or None
    lang_code = body.get("language") if body.get("language") in LANGS else "ru"
    language = LANGS[lang_code]
    include_files = bool(body.get("include_files"))

    if not _slots.acquire(blocking=False):
        return jsonify(error="busy"), 429
    try:
        with tempfile.TemporaryDirectory(prefix="lvdocs_") as tmp:
            counters, skipped, saved = {}, set(), 0
            for d in docs[:MAX_DOCS]:
                if not isinstance(d, dict):
                    continue
                stem = TYPE_TO_STEM.get(d.get("type"))
                if not stem:
                    skipped.add(str(d.get("type"))[:30])
                    continue
                m = DATA_URL.match(d.get("photo") or "")
                if not m:
                    continue
                raw = base64.b64decode(m.group(2))
                if len(raw) > MAX_IMG_BYTES:
                    continue
                counters[stem] = counters.get(stem, 0) + 1
                n = counters[stem]
                name = stem if n == 1 else f"{stem}_{n}"
                (Path(tmp) / f"{name}{EXT[m.group(1)]}").write_bytes(raw)
                saved += 1

            if not saved:
                return jsonify(error="no_documents", skipped=sorted(skipped)), 400

            results = checker.collect_package(tmp, merge_pages=True, parallel=True)
            category = checker.apply_rules(results, nationality)

            audit, audit_error = None, None
            try:
                audit = checker.audit_package(results, nationality, include_files, language)
            except Exception as e:                       # still return the document report
                app.logger.exception("audit failed")
                audit_error = str(e)

            results = translate_results(results, lang_code)      # messages in the interface language
            rmd_path = Path(tmp) / "report.Rmd"
            checker.To_rmd(results, str(rmd_path), audit_text=audit)
            rmd = rmd_path.read_text(encoding="utf-8")

            out = {
                name: {
                    "valid": r["valid"],
                    "missing": bool(r.get("missing")),
                    "errors": r["errors"],
                    "warnings": r["warnings"],
                    "data": checker._clean_data(r["data"]),
                } for name, r in results.items()
            }
            return jsonify(category=category, results=out, audit=audit,
                           audit_error=audit_error, rmd=rmd, skipped=sorted(skipped))
    except Exception:
        app.logger.exception("analyze failed")
        return jsonify(error="server"), 500
    finally:
        _slots.release()


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"LVDOCS.AI → http://{host}:{port}   (Gemini key: "
          f"{'OK' if checker.api_key_configured() else 'NOT SET — add GEMINI_API_KEY to .env'})")
    print(f"Models: reading = {checker.MODEL}, audit = {checker.AUDIT_MODEL}")
    app.run(host=host, port=port, threaded=True)
