"""utils — shared JSON storage helpers (hunts / leads / blacklists / portraits).

Provides a stable adapter over the per-hunt JSON layout so other layers do not
reach into ``api.hunt_store`` directly. Leads are addressed by a synthetic
``lead_key`` because lead records have no ``id`` field (see make_lead_key).
"""
