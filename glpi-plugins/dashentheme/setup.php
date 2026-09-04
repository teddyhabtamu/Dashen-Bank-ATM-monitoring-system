<?php
/**
 * Dashen Bank Theme for GLPI — applies Dashen branding (navy/gold) over
 * GLPI's default Tabler UI via a single stylesheet (dashen.css).
 *
 * Install: copy this folder to <glpi>/marketplace/dashentheme, then
 * Setup > Plugins > "Dashen Bank Theme" > Install + Enable.
 * (CLI alternative: php bin/console plugin:install dashentheme && php bin/console plugin:activate dashentheme)
 */

define('PLUGIN_DASHENTHEME_VERSION', '1.0.0');

function plugin_init_dashentheme() {
    global $PLUGIN_HOOKS;
    // Path is relative to this plugin's directory.
    $PLUGIN_HOOKS['add_css']['dashentheme'][] = 'dashen.css';
}

function plugin_version_dashentheme() {
    return [
        'name'           => 'Dashen Bank Theme',
        'version'        => PLUGIN_DASHENTHEME_VERSION,
        'author'         => 'Dashen Bank ATM Monitoring',
        'license'        => 'GPLv2+',
        'homepage'       => 'https://dashenbanksc.com',
        'requirements'   => [
            'glpi' => [
                'min' => '10.0',
            ],
        ],
    ];
}

function plugin_dashentheme_check_prerequisites() {
    return true;
}

function plugin_dashentheme_check_config() {
    return true;
}
