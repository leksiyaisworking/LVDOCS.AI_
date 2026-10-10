/* =========================================
   Добавление документа (check.html)
   ========================================= */
const currentPage = location.pathname.split('/').pop() || 'index.html';

let currentPhoto = null; // фото в виде dataURL (уменьшенное)

if (currentPage === 'check.html') {
    const fileInput = document.getElementById('fileInput');
    const zone      = document.getElementById('uploadZone');

    // клик по зоне не нужен: <label for="fileInput"> сам открывает диалог
    fileInput.addEventListener('change', () => {
        if (fileInput.files[0]) showImage(fileInput.files[0]);
    });

    ['dragenter', 'dragover'].forEach(evt =>
        zone.addEventListener(evt, (e) => {
            e.preventDefault();
            zone.classList.add('dragover');
        })
    );
    ['dragleave', 'drop'].forEach(evt =>
        zone.addEventListener(evt, (e) => {
            e.preventDefault();
            zone.classList.remove('dragover');
        })
    );
    zone.addEventListener('drop', (e) => {
        const file = e.dataTransfer.files[0];
        if (file && file.type.startsWith('image/')) showImage(file);
    });
}

// ==== Уменьшить фото, чтобы поместилось в localStorage (1400px — чтобы ИИ мог прочитать MRZ и мелкий текст) ====
function resizeImage(file, maxSize = 1400, quality = 0.78) {
    return new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const img = new Image();
        img.onload = () => {
            const scale = Math.min(1, maxSize / Math.max(img.width, img.height));
            const canvas = document.createElement('canvas');
            canvas.width  = Math.round(img.width * scale);
            canvas.height = Math.round(img.height * scale);
            canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
            URL.revokeObjectURL(url);
            resolve(canvas.toDataURL('image/jpeg', quality));
        };
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('image')); };
        img.src = url;
    });
}

// ==== Показать превью ====
async function showImage(file) {
    try {
        currentPhoto = await resizeImage(file);
    } catch {
        return;
    }
    document.getElementById('preview').src = currentPhoto;
    document.getElementById('previewWrap').style.display = 'block';
    document.getElementById('uploadZone').style.display = 'none';
    setMessage('');
}

function clearImage() {
    currentPhoto = null;
    document.getElementById('preview').removeAttribute('src');
    document.getElementById('previewWrap').style.display = 'none';
    document.getElementById('uploadZone').style.display = 'block';
    document.getElementById('fileInput').value = '';
}

// ==== Сообщение под кнопкой ====
function setMessage(html, color) {
    const box = document.getElementById('checkMsg');
    box.innerHTML = html ? `<p style="color:${color || '#c00'}; font-weight:600;">${html}</p>` : '';
}

// ==== Сохранить документ в профиль ====
function saveDocument() {
    const type = document.getElementById('docType').value;

    if (!currentPhoto) { setMessage('⚠️ ' + t('check.need_photo')); return; }
    if (!type)         { setMessage('⚠️ ' + t('check.need_type'));  return; }

    try {
        saveToProfile({
            id: 'doc_' + Date.now(),
            type: type,            // ключ типа, перевод берётся из doctype.<type>
            photo: currentPhoto
        });
    } catch (err) {
        console.error(err);
        setMessage('❌ ' + t('check.save_error'));
        return;
    }

    setMessage(`✔ ${t('check.saved')} &nbsp; <a href="profile.html" style="color:var(--latvia-red);">${t('check.go_profile')}</a>`, '#1a7a3a');
    clearImage();
    document.getElementById('docType').value = '';
}
