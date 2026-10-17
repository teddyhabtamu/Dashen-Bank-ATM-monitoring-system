#!/usr/bin/env bash
# Shared reader for config/portal_tables.txt.
#
# Usage:
#   source scripts/portal_tables.sh          # defines the functions
#   pg_dump $(portal_dump_flags) ...         # emits -t <table> for each
#   portal_table_names                        # one name per line
#   portal_table_count                        # count
#
# Deliberately sourced rather than executed so callers can compose the flags
# into their own command line.
#
# config/portal_tables.txt is the single list of record. It exists because
# backup_db.sh and split_portal_db.sh each carried their own list, drifted apart,
# and the full dump hid the gap until a scoped restore lost
# txn_source_policy - the table that decides whether simulated transactions are
# on screen.

PORTAL_TABLES_FILE="${PORTAL_TABLES_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/portal_tables.txt}"

portal_table_names() {
    # Strip comments and blanks; a table name per line is the whole contract.
    #
    # The order matters more than it looks. sed always newline-terminates its
    # output, so a list file whose last line has no trailing newline is still
    # fixed up here - which is what makes the grep safe. Reversing these two
    # steps, or replacing sed with a bare read loop, drops the last table from
    # every dump without a word of complaint.
    sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
        "$PORTAL_TABLES_FILE" 2>/dev/null \
        | grep -v '^$' || true
}

portal_table_count() {
    portal_table_names | wc -l | tr -d ' '
}

# One flag pair per line:
#   -t
#   atm_locations
# Callers turn this into arguments with: portal_dump_flags | tr '\n' ' '
portal_dump_flags() {
    portal_table_names | while read -r t; do
        printf -- '-t\n%s\n' "$t"
    done
}

# Which database actually holds the portal tables?
#
# Before the split that was "zabbix", where the portal shared a database with
# Zabbix's own configuration. Afterwards it is "portal". Hardcoding either is how
# a backup covers the wrong database: nothing fails, the file is written, the
# log looks normal, and portal data is simply absent until someone restores it.
#
# portal_schema_version is a portal-only marker created by txn_sources.py, and
# it travels with the copy - which is what makes it usable as the test. Returns
# empty when found in neither, so callers can refuse rather than guess.
detect_portal_db() {
    local candidate
    for candidate in portal zabbix; do
        if docker exec zabbix-db psql -U zabbix -d "$candidate" -tAc \
            "SELECT to_regclass('public.portal_schema_version') IS NOT NULL;" \
            2>/dev/null | grep -qx t; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}
