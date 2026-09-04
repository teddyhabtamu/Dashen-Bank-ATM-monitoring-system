/* Dashen Bank Theme — logo swap.
 * GLPI paints its mark through palette variables / generated content that
 * vary by page and palette, which proved fragile to override with CSS alone.
 * This replaces every logo span with a plain <img> of the bundled Dashen
 * lockup — deterministic on all pages (login + authenticated).
 */
(function () {
    var LOGO_URL = '/plugins/dashentheme/pics/dashen-bank-logo.png';

    function swapFavicon() {
        var link = document.querySelector('link[rel="shortcut icon"], link[rel="icon"]');
        if (link) {
            link.href = LOGO_URL;
        } else {
            link = document.createElement('link');
            link.rel = 'icon';
            link.type = 'image/png';
            link.href = LOGO_URL;
            document.head.appendChild(link);
        }
    }

    function swapLogos() {
        document.querySelectorAll('span.glpi-logo').forEach(function (el) {
            if (el.dataset.dashenDone) {
                return;
            }
            el.dataset.dashenDone = '1';
            el.style.background = 'none';
            var img = document.createElement('img');
            img.src = LOGO_URL;
            img.alt = 'Dashen Bank';
            img.style.cssText = 'max-width:100%;max-height:100%;width:auto;height:auto;';
            el.appendChild(img);
        });
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            swapFavicon();
            swapLogos();
        });
    } else {
        swapFavicon();
        swapLogos();
    }
})();
