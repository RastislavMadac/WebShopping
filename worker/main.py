import os
import sys
from dotenv import load_dotenv
from supabase import create_client, Client
from loguru import logger

# 1. VERZIOVANIE WORKERA
__version__ = "0.0.1"

# Nastavenie pekného logovania cez loguru
logger.remove()
logger.add(sys.stdout, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>Worker v{extra[version]}</cyan> - <white>{message}</white>")
logger = logger.bind(version=__version__)

# 2. BEZPEČNÉ NAČÍTANIE KĽÚČOV
# load_dotenv() načíta premenné zo skrytého .env súboru do os.environ
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Fail-fast ochrana: Ak kľúče chýbajú, worker okamžite spadne s chybou
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.error("Kritická chyba: Chýbajú Supabase kľúče! Skontroluj súbor .env.")
    sys.exit(1)

# 3. INICIALIZÁCIA SUPABASE KLIENTA
try:
    # Používame asynchrónneho klienta pre rýchlosť (ako kážu inštrukcie)
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.success("Worker úspešne spustený a pripojený k Supabase.")
except Exception as e:
    logger.error(f"Chyba pri pripájaní do databázy: {e}")
    sys.exit(1)

# TODO: Tu neskôr spustíme hlavný asynchrónny cyklus (ShoppingWorker)
if __name__ == "__main__":
    logger.info("Čakám na príkazy v nákupnej fronte...")