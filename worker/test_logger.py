import asyncio
import os
from dotenv import load_dotenv
from supabase import create_client

# Importujeme triedy z nášho hlavného súboru
from worker import SystemLogger, BaseStoreProvider

# Vytvoríme si na skúšku falošný obchod, aby sme splnili podmienky abstraktnej triedy
class TestProvider(BaseStoreProvider):
    async def initialize(self) -> None:
        pass
    
    async def search_product(self, query: str) -> list:
        return []
        
    async def add_to_cart(self, sku: str, quantity: int) -> bool:
        return True
        
    async def check_rate_limit(self) -> bool:
        return True

async def main():
    print("Načítavam prostredie...")
    load_dotenv()
    
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    
    # Pridaj tieto dva riadky, nech vidíme, čo Python naozaj vidí
    print(f"URL: {url}")
    print(f"KEY: {key[:10]}..." if key else "KEY: None")
    
    supabase = create_client(url, key)
    
    # 2. Inicializácia nášho nového Loggera
    logger = SystemLogger(supabase)
    
    # 3. Vytvorenie inštancie nášho testovacieho obchodu
    test_store = TestProvider("TEST_ESHOP_01", logger)
    
    # 4. Ideme logovať!
    print("Posielam logy do Supabase a terminálu...")
    
    await test_store.log_event("info", "Worker sa úspešne spustil.", {"version": "0.0.1"})
    await test_store.log_event("warning", "Pozor, toto je testovacie varovanie.")
    await test_store.log_event("error", "Simulujeme chybu! Akamai nás zablokoval.", {"url": "https://test.sk"})
    
    print("Hotovo! Logy by mali byť odoslané.")

if __name__ == "__main__":
    # Spustenie asynchrónnej funkcie
    asyncio.run(main())