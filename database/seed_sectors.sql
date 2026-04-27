-- Seed sector → macro driver mapping into system_config.
-- The F&O catalyst scorer reads 'sector_macro_map' to determine which macro
-- instruments are relevant for each equity sector.
-- Mirrors SECTOR_MACRO_MAP in src/collectors/macro_collector.py.
-- Safe to re-run (ON CONFLICT DO UPDATE preserves any manual overrides only if value differs).

INSERT INTO system_config (key, value, description) VALUES (
    'sector_macro_map',
    '{
        "Energy":         ["BRENT", "WTI"],
        "Oil & Gas":      ["BRENT", "WTI"],
        "Metals":         ["COPPER", "GOLD"],
        "Mining":         ["COPPER", "GOLD"],
        "Gold":           ["GOLD"],
        "FMCG":           ["DXY"],
        "IT":             ["NASDAQ_FUTURES", "DXY"],
        "Technology":     ["NASDAQ_FUTURES"],
        "Pharma":         ["DXY"],
        "Banking":        ["SPX_FUTURES", "DXY"],
        "Finance":        ["SPX_FUTURES"],
        "Auto":           ["SPX_FUTURES"],
        "Infrastructure": ["COPPER"],
        "Chemicals":      ["COPPER"],
        "Default":        ["SPX_FUTURES"]
    }',
    'Sector to macro driver mapping used by the F&O catalyst scorer'
) ON CONFLICT (key) DO NOTHING;
