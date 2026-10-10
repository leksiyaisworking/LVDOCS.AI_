/* =========================================
   LVDOCS.AI — светлая / тёмная тема
   Подключается в <head> ДО отрисовки, чтобы не было мигания.
   Без выбора пользователя тема следует за системной.
   ========================================= */
(function () {
    const KEY = 'lvdocs_theme';
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const LABELS = {
        ru: { light: 'Светлая тема', dark: 'Тёмная тема' },
        lv: { light: 'Gaišā tēma',  dark: 'Tumšā tēma' },
        en: { light: 'Light theme',  dark: 'Dark theme' }
    };

    function saved() {
        try { const v = localStorage.getItem(KEY); return v === 'light' || v === 'dark' ? v : null; }
        catch { return null; }
    }
    function current() { return saved() || (mq.matches ? 'dark' : 'light'); }

    function updateButton() {
        const btn = document.getElementById('themeBtn');
        if (!btn) return;
        const theme = current();
        const lang = (typeof getLang === 'function' && getLang()) || 'ru';
        const next = theme === 'dark' ? 'light' : 'dark';
        btn.textContent = theme === 'dark' ? '☀️' : '🌙';
        btn.title = btn.ariaLabel = (LABELS[lang] || LABELS.ru)[next];
    }

    function apply() {
        document.documentElement.dataset.theme = current();
        updateButton();
    }

    apply(); // сразу, в <head>

    mq.addEventListener('change', () => { if (!saved()) apply(); });

    document.addEventListener('DOMContentLoaded', () => {
        const nav = document.querySelector('.nav');
        if (nav && !document.getElementById('themeBtn')) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.id = 'themeBtn';
            btn.className = 'theme-btn';
            btn.addEventListener('click', () => {
                const next = current() === 'dark' ? 'light' : 'dark';
                try { localStorage.setItem(KEY, next); } catch {}
                apply();
            });
            nav.appendChild(btn);
        }
        updateButton();
    });
    document.addEventListener('langchange', updateButton);
})();
