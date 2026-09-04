# Dashen Bank Theme for GLPI (sample v1.0)

Applies Dashen branding — navy `#012169` / indigo `#273274` / gold `#FDD79A` —
to GLPI 10.x (Tabler UI): header, menu, buttons, cards, tables, tabs, forms,
Select2 dropdowns, badges, and the login page.

## Install

**Fresh deploy (compose handles files):** the `glpi` service mounts this folder
to both `marketplace/dashentheme` (registration) and `public/plugins/dashentheme`
(web serving — GLPI 10 serves plugin assets from there). After `docker compose up -d`:

```bash
# Enable it — either in the UI:
#   Setup > Plugins > "Dashen Bank Theme" > Install + Enable
# or via CLI (as www-data, not root):
docker exec -u www-data glpi php /var/www/html/glpi/bin/console plugin:install dashentheme
docker exec -u www-data glpi php /var/www/html/glpi/bin/console plugin:activate dashentheme
```

**Existing install (manual file copy):**

```bash
docker cp glpi-plugins/dashentheme glpi:/var/www/html/glpi/marketplace/dashentheme
docker exec glpi chown -R www-data:www-data /var/www/html/glpi/marketplace/dashentheme
# web-serving symlink (DocumentRoot is glpi/public):
docker exec glpi mkdir -p /var/www/html/glpi/public/plugins
docker exec glpi ln -sfn ../../marketplace/dashentheme /var/www/html/glpi/public/plugins/dashentheme
# then Install + Enable as above
```

Hard-refresh the browser (`Ctrl+Shift+R`). The `glpi-root` volume persists
everything, so the theme survives container recreates.

## Logo

The official Dashen mark ships with the plugin (`pics/dashen-logo.svg`,
`.png`, via ethiopianlogos open-source set) and is swapped in automatically
by §0 of the stylesheet — header + login page, no upload needed, works
offline. Uploading a different logo via **Setup > General > Logos** still
works as a manual override (it replaces the `<img>` the CSS hides).

## Verify / tune selectors

Rules marked `[CHECK]` in `dashen.css` are the few GLPI-specific selectors —
open devtools (F12), inspect the element, and adjust the selector if a rule
doesn't stick on your exact GLPI version. Everything under Tabler classes
(`.btn-primary`, `.card`, `.table`, `.nav-tabs`, `.form-control`,
`.select2-*`, `.badge`, `.navbar`) is version-stable.
