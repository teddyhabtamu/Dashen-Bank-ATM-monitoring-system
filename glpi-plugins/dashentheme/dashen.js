/* Dashen Bank Theme — logo swap.
 * GLPI paints its mark through palette variables / generated content that
 * vary by page and palette, which proved fragile to override with CSS alone.
 * This replaces every logo span with a plain <img> of the bundled Dashen
 * lockup — deterministic on all pages (login + authenticated).
 */
(function () {
    function swapLogos() {
        document.querySelectorAll('span.glpi-logo').forEach(function (el) {
            if (el.dataset.dashenDone) {
                return;
            }
            el.dataset.dashenDone = '1';
            el.style.background = 'none';
            var img = document.createElement('img');
            img.src = '/plugins/dashentheme/pics/dashen-bank-logo.png';
            img.alt = 'Dashen Bank';
            img.style.cssText = 'max-width:100%;max-height:100%;width:auto;height:auto;';
            el.appendChild(img);
        });
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', swapLogos);
    } else {
        swapLogos();
    }
})();
