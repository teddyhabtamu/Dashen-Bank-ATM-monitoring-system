#!/bin/sh
# Wait until the target layout file exists in the volume mapping
TARGET_FILE="/var/www/html/templates/layout/base.html.twig"

for i in $(seq 1 10); do
    if [ -f "$TARGET_FILE" ]; then
        # Check if the injection is already present to prevent duplicate appending
        if ! grep -q "brandfetch" "$TARGET_FILE"; then
            sed -i '/<\/head>/i <script>document.addEventListener("DOMContentLoaded",function(){document.querySelectorAll("link[rel*=\x27icon\x27]").forEach(i=>i.href=\x27https://cdn.brandfetch.io/idNZ7JoLsG/w/150/h/149/theme/dark/logo.png?c=1dxbfHSJFAPEGdCLU4o5B\x27);});</script>' "$TARGET_FILE"
        fi
        break
    fi
    sleep 2
done
