const menu = document.querySelector('[data-menu]');
const sidebar = document.querySelector('.sidebar');
const shade = document.querySelector('.mobile-shade');
function setMenu(open) {
  sidebar?.classList.toggle('is-open', open);
  shade?.classList.toggle('is-open', open);
  menu?.setAttribute('aria-expanded', String(open));
}
menu?.addEventListener('click', () => setMenu(menu.getAttribute('aria-expanded') !== 'true'));
shade?.addEventListener('click', () => setMenu(false));
document.addEventListener('keydown', event => { if (event.key === 'Escape') setMenu(false); });
document.querySelectorAll('[data-photo]').forEach(link => {
  link.addEventListener('click', event => {
    const dialog = document.querySelector('#photo-dialog');
    if (!dialog?.showModal) return;
    event.preventDefault();
    dialog.showModal();
  });
});
document.querySelector('[data-close-photo]')?.addEventListener('click', () => document.querySelector('#photo-dialog').close());
document.querySelectorAll('img[data-evidence]').forEach(img => {
  const showMissing = () => {
    img.closest('.photo-link').hidden = true;
    document.querySelector('[data-missing-photo]').hidden = false;
  };
  img.addEventListener('error', showMissing);
  if (img.complete && img.naturalWidth === 0) showMissing();
});
