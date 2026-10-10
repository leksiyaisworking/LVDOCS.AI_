/* =========================================
   LVDOCS.AI — профиль (localStorage)
   ========================================= */

const PROFILE_KEY = 'lvdocs_profile';

function getProfileDocuments() {
    try {
        return JSON.parse(localStorage.getItem(PROFILE_KEY)) || [];
    } catch {
        return [];
    }
}

function saveToProfile(doc) {
    const docs = getProfileDocuments();
    if (docs.some(d => d.id === doc.id)) return false;
    docs.push({ ...doc, savedAt: new Date().toISOString() });
    localStorage.setItem(PROFILE_KEY, JSON.stringify(docs));
    return true;
}

function removeFromProfile(id) {
    const docs = getProfileDocuments().filter(d => d.id !== id);
    localStorage.setItem(PROFILE_KEY, JSON.stringify(docs));
}

function clearProfile() {
    localStorage.removeItem(PROFILE_KEY);
}

function isInProfile(id) {
    return getProfileDocuments().some(d => d.id === id);
}
