/* =========================================
   LVDOCS.AI — ИИ-проверка пакета (analyze.html)
   Отправляет документы из профиля на /api/analyze
   ========================================= */
(function () {
    // типы профиля, для которых на сервере есть проверка (синхронно с backend/app.py)
    const ANALYZABLE = new Set(['passport', 'diploma', 'transcript', 'criminal', 'bank_statement',
        'pmlp_invitation', 'study_agreement', 'school_certificate', 'birth_cert',
        'visa_c', 'visa_d', 'insurance']);

    const $ = (id) => document.getElementById(id);
    let state = { data: null, error: null, busy: false, serverOk: null };

    const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
        (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    // ---------- мини-рендер Markdown (заголовки, списки, таблицы, **жирный**) ----------
    function md(src) {
        const inline = (s) => s.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`(.+?)`/g, '<code>$1</code>');
        const lines = esc(src).split('\n');
        let out = '', i = 0;
        while (i < lines.length) {
            const line = lines[i];
            if (/^\s*\|/.test(line)) {
                const rows = [];
                while (i < lines.length && /^\s*\|/.test(lines[i])) {
                    if (!/^\s*\|[\s:\-|]+\|?\s*$/.test(lines[i]))
                        rows.push(lines[i].trim().replace(/^\||\|$/g, '').split('|').map((c) => inline(c.trim())));
                    i++;
                }
                if (rows.length) {
                    out += '<div class="an-scroll"><table class="an-table"><thead><tr>' +
                        rows[0].map((c) => `<th>${c}</th>`).join('') + '</tr></thead><tbody>' +
                        rows.slice(1).map((r) => '<tr>' + r.map((c) => `<td>${c}</td>`).join('') + '</tr>').join('') +
                        '</tbody></table></div>';
                }
                continue;
            }
            let m;
            if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
                const lvl = Math.min(m[1].length + 2, 5);
                out += `<h${lvl}>${inline(m[2])}</h${lvl}>`; i++; continue;
            }
            if (/^\s*[*\-]\s+/.test(line)) {
                out += '<ul>';
                while (i < lines.length && /^\s*[*\-]\s+/.test(lines[i]))
                    out += `<li>${inline(lines[i++].replace(/^\s*[*\-]\s+/, ''))}</li>`;
                out += '</ul>'; continue;
            }
            if (/^\s*\d+[.)]\s+/.test(line)) {
                out += '<ol>';
                while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i]))
                    out += `<li>${inline(lines[i++].replace(/^\s*\d+[.)]\s+/, ''))}</li>`;
                out += '</ol>'; continue;
            }
            if (line.trim()) out += `<p>${inline(line)}</p>`;
            i++;
        }
        return out;
    }

    // ---------- сохранённые документы ----------
    function renderSaved() {
        const docs = getProfileDocuments();
        const box = $('anSaved');
        if (!docs.length) {
            box.innerHTML = `<p style="color:var(--text-gray)">${t('analyze.none')}</p>
                <p><a class="btn" href="check.html">${t('analyze.add')}</a></p>`;
        } else {
            const counts = {};
            docs.forEach((d) => { if (d.type) counts[d.type] = (counts[d.type] || 0) + 1; });
            const chips = Object.entries(counts).map(([type, n]) => {
                const ok = ANALYZABLE.has(type);
                return `<li class="an-chip${ok ? '' : ' off'}">${esc(t('doctype.' + type))}
                    ${n > 1 ? `<span>×${n}</span>` : ''}</li>`;
            }).join('');
            const skipped = Object.keys(counts).filter((x) => !ANALYZABLE.has(x));
            box.innerHTML = `<ul class="an-chips">${chips}</ul>` +
                (skipped.length ? `<p class="an-note">${t('analyze.skipped')} ${skipped.map((x) => esc(t('doctype.' + x))).join(', ')}</p>` : '');
        }
        updateButton();
    }

    function updateButton() {
        const has = getProfileDocuments().some((d) => ANALYZABLE.has(d.type));
        $('anRun').disabled = state.busy || !has || state.serverOk === false;
    }

    function banner(msg) {
        $('anBanner').innerHTML = msg ? `<div class="docs-intro" style="border-left-color:#c00;">⚠️ ${esc(msg)}</div>` : '';
    }

    async function checkServer() {
        try {
            const r = await fetch('/api/health', { cache: 'no-store' });
            const j = await r.json();
            state.serverOk = !!j.ok;
            state.keyOk = !!j.gemini_key;
            banner(state.keyOk ? '' : t('analyze.err.no_key'));
        } catch {
            state.serverOk = false;
            banner(t('analyze.no_server'));
        }
        updateButton();
    }

    // ---------- результат ----------
    function renderResult() {
        const box = $('anResult');
        if (state.error) { box.innerHTML = `<div class="docs-intro" style="border-left-color:#c00;">❌ ${esc(state.error)}</div>`; return; }
        const d = state.data;
        if (!d) { box.innerHTML = ''; return; }

        const cards = Object.entries(d.results).map(([name, r]) => {
            const bad = r.errors.length > 0;
            const cls = r.missing ? 'miss' : bad ? 'bad' : r.warnings.length ? 'warn' : 'ok';
            const label = r.missing ? t('analyze.missing') : bad ? t('analyze.fail') : t('analyze.ok');
            const list = (title, arr) => arr.length
                ? `<p class="an-sub">${title}</p><ul>${arr.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : '';
            const hasData = r.data && Object.keys(r.data).length;
            return `<div class="an-doc ${cls}">
                <div class="an-doc-head"><h3>${esc(name)}</h3><span class="an-badge ${cls}">${label}</span></div>
                ${list(t('analyze.errors'), r.errors)}${list(t('analyze.warnings'), r.warnings)}
                ${hasData ? `<details><summary>${t('analyze.details')}</summary>
                    <pre class="ocr-text">${esc(JSON.stringify(r.data, null, 2))}</pre></details>` : ''}
            </div>`;
        }).join('');

        const audit = d.audit
            ? `<div class="an-audit">${md(d.audit)}</div>`
            : `<div class="docs-intro" style="border-left-color:#c00;">${t('analyze.audit_failed')} ${esc(d.audit_error || '')}</div>`;

        box.innerHTML = `
            <h2 style="margin-top:40px;">${t('analyze.audit')}</h2>
            <p class="an-cat"><b>${t('analyze.category')}:</b> ${esc(d.category)}</p>
            ${audit}
            <div class="an-actions">
                ${d.audit ? `<button type="button" class="btn btn-secondary" data-dl="md">${t('analyze.dl_md')}</button>` : ''}
                <button type="button" class="btn btn-secondary" data-dl="rmd">${t('analyze.dl_rmd')}</button>
            </div>
            <h2 style="margin-top:40px;">${t('analyze.result')}</h2>
            <div class="an-docs">${cards}</div>`;
    }

    function download(name, text, mime) {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([text], { type: mime + ';charset=utf-8' }));
        a.download = name;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    }

    // ---------- запуск ----------
    async function run() {
        if (!$('anConsent').checked) { $('anStatus').textContent = '⚠️ ' + t('analyze.need_consent'); return; }
        const documents = getProfileDocuments()
            .filter((d) => ANALYZABLE.has(d.type) && d.photo)
            .map((d) => ({ type: d.type, photo: d.photo }));

        state = { ...state, busy: true, data: null, error: null };
        updateButton(); renderResult();
        $('anStatus').textContent = '⏳ ' + t('analyze.running');
        try {
            const r = await fetch('/api/analyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    documents,
                    nationality: $('anNat').value.trim(),
                    language: getLang(),
                    include_files: $('anInclude').checked
                })
            });
            const j = await r.json().catch(() => ({}));
            if (!r.ok) {
                const key = 'analyze.err.' + (j.error || 'server');
                state.error = t(key) === key ? t('analyze.err.server') : t(key);
            } else {
                state.data = j;
            }
        } catch {
            state.error = t('analyze.err.network');
        }
        state.busy = false;
        $('anStatus').textContent = '';
        updateButton(); renderResult();
        if (state.data) $('anResult').scrollIntoView({ behavior: 'smooth' });
    }

    document.addEventListener('DOMContentLoaded', () => {
        $('anRun').addEventListener('click', run);
        $('anResult').addEventListener('click', (e) => {
            const b = e.target.closest('[data-dl]');
            if (!b || !state.data) return;
            if (b.dataset.dl === 'md') download('lvdocs-audit.md', state.data.audit, 'text/markdown');
            else download('lvdocs-report.Rmd', state.data.rmd, 'text/plain');
        });
        renderSaved(); checkServer();
    });
    document.addEventListener('langchange', () => {
        renderSaved(); renderResult();
        if (state.serverOk === false) banner(t('analyze.no_server'));
        else if (state.keyOk === false) banner(t('analyze.err.no_key'));
    });
    window.addEventListener('pageshow', renderSaved);
})();
