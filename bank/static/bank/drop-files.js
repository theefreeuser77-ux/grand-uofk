// Drag-and-drop for every file picker on the site.
// Dropping files on a picker's area puts them into that picker and fires "change", so each
// page's existing upload code (image shrinking, matching, previews) runs exactly as if the
// files had been chosen with the button. The area is the nearest [data-drop-target], else the
// picker's <label>, else its parent element.
(() => {
  if (!window.DataTransfer) return;

  const zoneFor = input => input.closest('[data-drop-target]') || input.closest('label') || input.parentElement;
  const zones = new Map();  // zone element -> file input

  function register(root = document) {
    root.querySelectorAll('input[type=file]:not([disabled])').forEach(input => {
      const zone = zoneFor(input);
      if (zone && !zones.has(zone)) {
        zones.set(zone, input);
        zone.classList.add('drop-zone');
      }
    });
  }

  const hasFiles = event => [...((event.dataTransfer && event.dataTransfer.types) || [])].includes('Files');
  const zoneAt = target => {
    for (let el = target; el && el !== document; el = el.parentElement) {
      if (zones.has(el)) return el;
    }
    return null;
  };

  // "image/*", ".txt,text/plain" -> does this file match?
  function accepts(input, file) {
    const rules = (input.accept || '').split(',').map(rule => rule.trim().toLowerCase()).filter(Boolean);
    if (!rules.length) return true;
    const name = file.name.toLowerCase(), type = (file.type || '').toLowerCase();
    return rules.some(rule => rule.startsWith('.') ? name.endsWith(rule)
      : rule.endsWith('/*') ? type.startsWith(rule.slice(0, -1))
      : type === rule);
  }

  function flash(zone, className) {
    zone.classList.add(className);
    setTimeout(() => zone.classList.remove(className), 1600);
  }

  let current = null;
  function highlight(zone) {
    if (current === zone) return;
    if (current) current.classList.remove('drop-active');
    current = zone;
    if (zone) zone.classList.add('drop-active');
  }

  document.addEventListener('dragover', event => {
    if (!hasFiles(event)) return;
    event.preventDefault();  // never let the browser open a dropped file in place of the page
    const zone = zoneAt(event.target);
    event.dataTransfer.dropEffect = zone ? 'copy' : 'none';
    highlight(zone);
  });
  document.addEventListener('dragleave', event => {
    if (!event.relatedTarget) highlight(null);  // pointer left the window
  });
  document.addEventListener('drop', event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    highlight(null);
    const zone = zoneAt(event.target);
    if (!zone) return;
    const input = zones.get(zone);
    const files = [...event.dataTransfer.files].filter(file => accepts(input, file));
    if (!files.length) {
      flash(zone, 'drop-rejected');
      return;
    }
    const transfer = new DataTransfer();
    (input.multiple ? files : files.slice(0, 1)).forEach(file => transfer.items.add(file));
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', {bubbles: true}));
    flash(zone, 'drop-done');
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => register());
  else register();
  window.registerFileDrops = register;  // pages that add pickers later can call this
})();
