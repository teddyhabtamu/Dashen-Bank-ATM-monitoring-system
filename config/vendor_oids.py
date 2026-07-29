"""
config/vendor_oids.py — Vendor OID mapping for SNMP monitoring.

This module maps vendor names to their enterprise OIDs and
provides a mapping between simulation OIDs and production OIDs.

Usage:
    from config.vendor_oids import get_oid_root, get_production_oid

    oid_root = get_oid_root('NCR')  # '1.3.6.1.4.1.37513'
    oid_root = get_oid_root('GRG')  # '1.3.6.1.4.1.51234'
"""

# ─── SIMULATION OID ROOT (synthetic) ────────────────────────────────────────
SIM_OID_ROOT = '1.3.6.1.4.1.99999'

# ─── VENDOR PRODUCTION OID ROOTS ────────────────────────────────────────────
VENDOR_OIDS = {
    'NCR': '1.3.6.1.4.1.37513',
    'GRG': '1.3.6.1.4.1.51234',
}

# ─── OID TO STATE KEY MAPPING (simulation) ──────────────────────────────────
# These are the OIDs used by the simulator (1.3.6.1.4.1.99999.*)
# In production, the same state keys are used but with different OID roots.

NCR_OID_MAP = {
    '1.1.0': 'atm_status',
    '1.2.0': 'cassette1',
    '1.3.0': 'cassette2',
    '1.4.0': 'cassette3',
    '1.5.0': 'cassette4',
    '1.6.0': 'reject_bin',
    '1.7.0': 'cash_jam',
    '1.8.0': 'partial_dispense',
    '2.1.0': 'card_reader',
    '2.2.0': 'card_captures',
    '2.3.0': 'shutter',
    '3.1.0': 'receipt_printer',
    '3.2.0': 'receipt_paper',
    '3.3.0': 'journal_printer',
    '4.1.0': 'safe_door',
    '4.2.0': 'cabinet_door',
    '4.3.0': 'temperature',
    '4.4.0': 'humidity',
    '4.5.0': 'vibration',
    '4.6.0': 'intrusion',
    '5.1.0': 'txn_total',
    '5.2.0': 'txn_failed',
    '5.3.0': 'txn_success',
    '5.4.0': 'last_error',
    '6.1.0': 'main_power',
    '6.2.0': 'ups_status',
    '6.3.0': 'ups_battery',
    '6.4.0': 'last_power_event',
    '7.1.0': 'net_link',
    '7.2.0': 'net_latency',
    '7.3.0': 'packet_loss',
    '7.4.0': 'link_type',
    '8.1.0': 'camera1',
    '8.2.0': 'camera2',
    '8.3.0': 'cam_storage',
}

GRG_OID_MAP = {
    '1.1.0': 'atm_status',
    '2.1.0': 'cash_module1',
    '2.2.0': 'cash_module2',
    '2.3.0': 'cash_module3',
    '2.4.0': 'purge_bin',
    '2.5.0': 'cash_jam',
    '3.1.0': 'card_unit',
    '3.2.0': 'card_captures',
    '4.1.0': 'thermal_printer',
    '4.2.0': 'paper_level',
    '5.1.0': 'safe_door',
    '5.2.0': 'top_hat',
    '5.3.0': 'temperature',
    '5.4.0': 'humidity',
    '6.1.0': 'txn_total',
    '6.2.0': 'txn_failed',
    '6.3.0': 'txn_success',
    '7.1.0': 'ups_status',
    '7.2.0': 'ups_battery',
    '8.1.0': 'net_link',
    '8.2.0': 'net_latency',
    '8.3.0': 'packet_loss',
    '9.1.0': 'camera1',
    '9.2.0': 'cam_storage',
}

def get_oid_root(vendor):
    """Get the OID root for a vendor."""
    return VENDOR_OIDS.get(vendor, SIM_OID_ROOT)

def get_production_oid(vendor, oid_suffix):
    """Get the production OID for a vendor and suffix.
    
    Args:
        vendor: 'NCR' or 'GRG'
        oid_suffix: e.g., '1.1.0' for ATM status
    
    Returns:
        Full OID string, e.g., '1.3.6.1.4.1.37513.1.1.0' for NCR
    """
    root = get_oid_root(vendor)
    return f"{root}.{oid_suffix}"

def get_sim_oid(oid_suffix):
    """Get the simulation OID for a suffix.
    
    Args:
        oid_suffix: e.g., '1.1.0' for ATM status
    
    Returns:
        Full OID string, e.g., '1.3.6.1.4.1.99999.1.1.0'
    """
    return f"{SIM_OID_ROOT}.{oid_suffix}"
