-- Vytvorenie unikátneho indexu pre EAN (čiarový kód) v tabuľke sku_variants
CREATE UNIQUE INDEX IF NOT EXISTS idx_sku_variants_barcode 
ON public.sku_variants (barcode);

-- Pridanie komentára pre lepšiu prehľadnosť
COMMENT ON INDEX public.idx_sku_variants_barcode IS 'Unikátny index pre rýchle vyhľadávanie a zabránenie duplicitám čiarových kódov (EAN).';