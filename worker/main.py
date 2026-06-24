import asyncio
import re
import os
import random
import urllib.parse
import time
import json
import math
import sys
from abc import ABC, abstractmethod
from dotenv import load_dotenv
from supabase import create_client, Client 
from supabase import create_async_client
from playwright.async_api import async_playwright
from supabase import create_client, Client
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from loguru import logger
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
import httpx

import asyncio

# ==========================================
# 1. KONFIGURÁCIA SUPABASE A WORKERA
# ==========================================
# NOVÉ: Nastavenie verzie pre logovanie a deployment
WORKER_VERSION = "worker-v0.0.2"

# ==========================================
# 1. KONFIGURÁCIA SUPABASE
# ==========================================
# ⬅️ NOVÉ: Načítanie tajných kľúčov zo súboru .env
# TOTO JE TEN TRIK: override=True prinúti Python prečítať opravený .env súbor
load_dotenv(override=True)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("CRITICAL ERROR: Chýbajú Supabase kľúče v .env súbore!")
    sys.exit(1)

# Pripravíme inštanciu Supabase pre celý worker
supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)

class SystemLogger:
    """
    Centrálny logger pre workera. 
    Zapisuje farebne do konzoly (cez loguru) a asynchrónne do Supabase DB (pre UI toasty).
    """
    def __init__(self, supabase_client):
        self.supabase = supabase_client
        self.version = WORKER_VERSION
        
        # ⬅️ NOVÉ: Toto zabezpečí, že obyčajné logger.success() nespadne na KeyError
        logger.configure(extra={"version": self.version, "extra_context": ""})
        
        # Reset loguru a nastavenie formátu
        logger.remove()
        logger.add(
            sys.stdout, 
            colorize=True, 
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <magenta>[{extra[version]}]</magenta> | <cyan>{message}</cyan> <dim>{extra[extra_context]}</dim>"
        )
        
        # Pevne naviažeme (bind) našu verziu do lokálneho loggera
        self.local_logger = logger.bind(version=self.version, extra_context="")

    async def log(self, level: str, message: str, context: Optional[Dict] = None):
        """Zaznamená log do konzoly a odošle do databázy."""
        if context is None:
            context = {}
            
        # ⬅️ NOVÉ: Do každého logu, ktorý ide do Supabase (a následne Angularu), pribalíme verziu!
        context["worker_version"] = self.version
            
        # 1. Výpis do lokálnej konzoly (Loguru)
        loguru_level = level.upper()
        if loguru_level == "ERROR":
            self.local_logger.bind(extra_context=context).error(message)
        elif loguru_level == "WARNING":
            self.local_logger.bind(extra_context=context).warning(message)
        else:
            self.local_logger.bind(extra_context=context).info(message)

        # 2. Zápis do Supabase databázy (Worker Logs)
        try:
            self.supabase.table('worker_logs').insert({
                "level": level.lower(),
                "message": message,
                "context": context
            }).execute()
        except Exception as e:
            # Ak zlyhá zápis do DB, vypíšeme to len lokálne
            self.local_logger.error(f"Zlyhal zápis logu do databázy: {e}")

# ==========================================
# 2. ZÁKLADNÉ ROZHRANIE PRE PROVIDEROV (Provider Interface)
# ==========================================


class BaseStoreProvider(ABC):
    """
    Abstraktná trieda, ktorú musí implementovať každý e-shop (Metro, Tesco, atď.).
    ShoppingWorker vďaka tomu komunikuje s akýmkoľvek obchodom rovnakým spôsobom.
    """

    def __init__(self, store_id: str, logger_instance):
        """
        Inicializácia providera s ID obchodu a centrálnym loggerom pre UI toasty.
        """
        self.store_id = store_id
        self.logger = logger_instance
        self.logger_context = {"store_id": store_id}

    async def log_event(self, level: str, message: str, context: Optional[Dict] = None):
        """
        Pomocná metóda na bezpečný zápis logov do terminálu aj do Supabase (pre Angular UI).
        """
        full_context = self.logger_context.copy()
        if context:
            full_context.update(context)
        
        # Posielame log do hlavného SystemLoggera
        await self.logger.log(level, message, full_context)

    @abstractmethod
    async def initialize(self) -> None:
        """Inicializuje HTTP klienta, načíta cookies / session tokeny."""
        pass

    @abstractmethod
    async def search_product(self, query: str) -> List[Dict[str, Any]]:
        """Vyhľadá produkt a vráti jednotný zoznam znormalizovaných slovníkov."""
        pass

    @abstractmethod
    async def add_to_cart(self, sku: str, quantity: int) -> bool:
        """Pridá položku do košíka a vráti True/False podľa úspešnosti."""
        pass

    @abstractmethod
    async def check_rate_limit(self) -> bool:
        """Detekcia 429 Too Many Requests alebo blokácie (napr. Akamai / Cloudflare)."""
        pass

# ==========================================
# 3. ZÍSKANIE COOKIES CEZ PLAYWRIGHT (Metro špecifické)
# ==========================================
async def get_metro_cookies() -> dict:
    print("\n[LOGIN] 🔓 Zahrievam prehliadač a získavam čerstvé prístupy pre Metro...")
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir="./metro_chrome_profil",
            headless=False 
        )
        page = context.pages[0]
        
        try:
            from playwright_stealth import stealth_async
            await stealth_async(page)
        except ImportError:
            pass

        await page.goto("https://www.metro.sk/", wait_until="commit", timeout=15000)
        await page.wait_for_timeout(3000)

        if "idam.metro.sk" in page.url:
            print("⚠️ POZOR: Tvoje prihlásenie vypršalo alebo ťa zablokovali!")
            print("👉 Prehliadač je otvorený. Prosím, prihlás sa manuálne alebo vyklikaj CAPTCHU.")
            print("⏳ Skript čaká na úspešné overenie...")
            
            while "idam.metro.sk" in page.url or "challenge" in page.url.lower():
                await page.wait_for_timeout(2000)
            
            print("✅ Úspešne overený/prihlásený!")

        playwright_cookies = await context.cookies()
        httpx_cookies = {cookie['name']: cookie['value'] for cookie in playwright_cookies}
        
        print("[LOGIN] 🔐 Cookies úspešne načítané. Zatváram prehliadač.\n")
        await context.close()
        return httpx_cookies


# ==========================================
# 4. METRO PROVIDER (Implementácia rozhrania)
# ==========================================
class MetroProvider(BaseStoreProvider):
    def __init__(self, logger_instance):
        # Voláme rodičovský konštruktor s ID Metra a centrálnym loggerom
        super().__init__("00021", logger_instance)
        
        # Odkazy na Playwright inštancie, ktoré budeme recyklovať
        self.playwright = None
        self.browser_context = None
        self.page = None
        
        # URL adresy
        self.base_url = "https://www.metro.sk/"
        
        # Throttling a ochrana
        self.base_delay = 1.0  # Dynamický delay pre _apply_jitter (využijeme neskôr)

    async def initialize(self) -> None:
        """
        Spustí persistentný Playwright, aplikuje stealth a vyrieši Akamai / Login.
        Kontext zostáva otvorený pre metódy search_product a add_to_cart.
        """
        await self.log_event("info", "🚀 [METRO] Inicializujem Playwright prehliadač (Stealth mód)...")
        
        # 1. Štart Playwrightu
        self.playwright = await async_playwright().start()
        
        # 2. Vytvorenie / Načítanie persistentného kontextu (zachováva session a cookies)
        self.browser_context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir="./metro_chrome_profil",
            headless=False,  # Zatiaľ necháme False, aby si videl, čo sa deje v DB/UI
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--start-maximized"
            ],
            viewport={"width": 1920, "height": 1080}
        )
        
        # 3. Získanie prvej záložky (alebo vytvorenie novej)
        if self.browser_context.pages:
            self.page = self.browser_context.pages[0]
        else:
            self.page = await self.browser_context.new_page()
            
        # 4. Aplikácia Stealth ochrany (obchádzanie základných detekcií botov)
        await Stealth().apply_stealth_async(self.page)
        
        await self.log_event("info", "🌐 [METRO] Prechádzam na hlavnú stránku a overujem session...")
        
        # 5. Navigácia na Metro a čakanie na Akamai validáciu
        try:
            await self.page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)  # Krátka pauza na usadenie DOMu a JS
            
            # 6. Kontrola presmerovania na Login alebo Akamai Challenge
            current_url = self.page.url
            if "idam.metro.sk" in current_url or "challenge" in current_url.lower():
                await self.log_event("warning", "⚠️ [METRO] Detekovaný Login alebo Akamai Challenge! Čakám na vyriešenie...")
                
                # Worker sa nezasekne, ale bude čakať, kým sa nedostaneme z login/challenge obrazovky
                while "idam.metro.sk" in self.page.url or "challenge" in self.page.url.lower():
                    await asyncio.sleep(2)
                
                await self.log_event("success", "✅ [METRO] Úspešne prihlásené / overené Akamai!")
            else:
                await self.log_event("success", "✅ [METRO] Session je platná, prístup povolený bez výzvy.")
                
        except Exception as e:
            await self.log_event("error", f"❌ [METRO] Zlyhala inicializácia stránky: {str(e)}")
            raise e
            
    # Dočasné mocky pre splnenie rozhrania (doplníme v ďalšom kroku)
    async def search_product(self, query: str) -> list:
        """
        Vyhľadá produkt pomocou natívneho JS fetch priamo z otvorenej stránky Metra.
        Obsahuje auto-throttling a ochranu voči 429 / 403.
        """
        import urllib.parse
        import asyncio
        import random
        
        # 1. Throttling a Jitter (ochrana proti rate-limitingu)
        delay = self.base_delay + random.uniform(0.5, 2.0)
        await asyncio.sleep(delay)
        
        bezpecna_url = urllib.parse.quote(query)
        legacy_url = f"https://www.metro.sk/metro/UnifiedSearch/Search?searchType=mshop&storeId={self.store_id}&searchTerm={bezpecna_url}"
        
        await self.log_event("info", f"🔍 [METRO] Hľadám produkt: '{query}'... (Aktuálny delay: {self.base_delay:.1f}s)")
        
        try:
            # 2. Spustíme natívny JS fetch vo vnútri nášho overeného Playwright kontextu
            json_response = await self.page.evaluate(f"""
                async () => {{
                    const response = await fetch("{legacy_url}", {{
                        headers: {{
                            "X-Requested-With": "XMLHttpRequest",
                            "Accept": "application/json, text/plain, */*"
                        }}
                    }});
                    
                    // Ak server vráti 403, 401 alebo 429, pošleme si to späť do Pythonu
                    if (!response.ok) {{
                        return {{ error: true, status: response.status }};
                    }}
                    
                    return await response.json();
                }}
            """)
            
            # 3. KONTROLA CHÝB A ANTI-BOT OCHRANA
            if isinstance(json_response, dict) and json_response.get("error"):
                status = json_response.get("status")
                await self.log_event("error", f"❌ [METRO] Blokácia API! HTTP Status: {status}")
                
                # A) Záchranné koleso - HTTP 401/403 (Vyhodili nás / Expirovaná session)
                if status in [401, 403]:
                    await self.log_event("warning", "🔄 Re-inicializujem prehliadač kvôli exspirácii...")
                    await self.initialize()
                    return await self.search_product(query) 
                
                # B) Auto-Throttling - HTTP 429 (Too Many Requests - Sme príliš rýchli)
                if status == 429:
                    # Exponential Backoff: Zdvojnásobíme delay, maximálne však na 30 sekúnd
                    self.base_delay = min(self.base_delay * 2.0, 30.0)
                    await self.log_event("warning", f"🐢 [THROTTLING] Dostali sme 429. Spomaľujem workera na {self.base_delay:.1f}s a čakám...")
                    
                    await asyncio.sleep(self.base_delay)
                    return await self.search_product(query) # Skúsime znova s novým (dlhším) delayom
                    
                return []
                
            # 4. POSTUPNÉ ZRÝCHĽOVANIE (Ramp-down)
            # Ak sme mali zvýšený delay, ale teraz to prešlo v poriadku, postupne sa vraciame do normálu
            if self.base_delay > 1.0:
                stary_delay = self.base_delay
                self.base_delay = max(1.0, self.base_delay * 0.8) # Znížime o 20%
                await self.log_event("info", f"🐇 [THROTTLING] API je stabilné. Znižujem delay z {stary_delay:.1f}s na {self.base_delay:.1f}s.")

            # 5. Parsovanie úspešných dát (JSONa / JSONb)
            import json
            with open(f"DEBUG_METRO_{query.replace(' ', '_')}.json", "w", encoding="utf-8") as f:
                json.dump(json_response, f, indent=4, ensure_ascii=False)
                
            produkty = self._normalize_json(json_response)
            
            await self.log_event("success", f"✅ [METRO] Našiel som {len(produkty)} znormalizovaných produktov.")
            return produkty
            
        except Exception as e:
            await self.log_event("error", f"❌ [METRO] Zlyhalo hľadanie: {str(e)}")
            return []
    async def add_to_cart(self, sku: str, quantity: int) -> bool:
        return True

    async def check_rate_limit(self) -> bool:
        return True
    
    # =========================================================================
    # POMOCNÉ METÓDY PRE PARSOVANIE A NORMALIZÁCIU DÁT (Zosúladené s Röntgenom)
    # =========================================================================
    def _normalize_json(self, json_data: dict) -> list:
        """Rozpozná formát odpovede (JSONa vs JSONb) a nasmeruje ho na správny parser."""
        if "Results" in json_data:
            return self._spracuj_format_a(json_data["Results"])
        elif "results" in json_data:
            return self._spracuj_format_b(json_data["results"])
        elif "result" in json_data:
            return self._spracuj_format_b(json_data["result"])
        elif "articles" in json_data:
            return self._spracuj_format_b(json_data["articles"])
        else:
            logger.warning(f"⚠️ [METRO] API vrátilo neznámu štruktúru. Dostupné kľúče: {list(json_data.keys())}")
            return []

    def _spracuj_format_a(self, results_list: list) -> list:
        """Spracuje zjednodušený formát JSONa (Legacy API / UnifiedSearch)."""
        normalizovane = []
        for item in results_list:
            if not item: 
                continue 
            
            nazov = item.get("Name", "")
            is_available = item.get("IsAvailable", False)
            status = str(item.get("AvailabilityDetails", "")).upper()
            
            na_sklade = True
            if not is_available and "AVAILABLE" not in status and "SKLADOM" not in status:
                na_sklade = False

            sku = item.get("ID", "")
            url = item.get("TargetUrl", "") or f"https://sortiment.metro.sk/shop/pv/{sku}"
            
            img_url = item.get("ImageUrl") or item.get("Image") or item.get("MainImage") or ""
            if img_url and not img_url.startswith("http"):
                img_url = "https://www.metro.sk" + ("" if img_url.startswith("/") else "/") + img_url

            price_info = item.get("PriceInfo") or {}
            cena_str = price_info.get("SecondSegment", "0")
            
            # ---------------------------------------------------------
            # OPRAVA 1: Bezpečný Regex (Zoberie LEN prvé skutočné číslo)
            # ---------------------------------------------------------
            cena_clean = cena_str.replace(" ", "").replace("\xa0", "")
            match = re.search(r'(\d+(?:[.,]\d+)?)', cena_clean)
            if not match:
                continue
            
            vyextrahovana_hodnota = float(match.group(1).replace(",", "."))

            vaha_kg, pocet_kusov = self._extract_weight_and_pieces(nazov)
            if vaha_kg <= 0: 
                continue
            
            # ---------------------------------------------------------
            # OPRAVA 2: Trojitá nepriestrelná detekcia "Ceny za Kilo"
            # ---------------------------------------------------------
            is_per_kg = False
            price_info_str = str(price_info).lower()
            
            # 1. Je v názve "váž." (vážené produkty sú vždy nacenené na kilo)
            if "váž." in nazov.lower() or "vážené" in nazov.lower() or "vaz." in nazov.lower():
                is_per_kg = True
            # 2. Poslalo API explicitne UnitType: "kg"?
            elif str(price_info.get("UnitType", "")).lower() == "kg":
                is_per_kg = True
            # 3. Nachádza sa niekde v celom bloku ceny text "/kg"?
            elif "/kg" in price_info_str or "\\/kg" in price_info_str:
                is_per_kg = True

            # Finálna matematika na základe detekcie
            if is_per_kg:
                cena_za_kg = vyextrahovana_hodnota
                cena_balenia = vyextrahovana_hodnota * vaha_kg  # Napr: 9.49 * 3.2 kg = 30.37 € za kus mäsa
            else:
                cena_balenia = vyextrahovana_hodnota
                cena_za_kg = cena_balenia / vaha_kg if vaha_kg > 0 else 0.0
            
            normalizovane.append({
                "sku": sku, 
                "name": nazov, 
                "cena_balenia": round(cena_balenia, 2),
                "cena_za_kg": round(cena_za_kg, 2),
                "vaha_kg": vaha_kg, 
                "url": url, 
                "image_url": img_url, 
                "in_stock": na_sklade,
                "kusov_v_baleni": pocet_kusov,
                "zdroj_api": "JSONa (Staré API Metro)"
            })
        return normalizovane
    def _spracuj_format_b(self, result_dict: dict) -> list:
        """Spracuje komplexný formát JSONb (Betty API - Variants/Bundles)."""
        normalizovane = []
        for article_id, article in result_dict.items():
            article_image = article.get("imageUrl") or article.get("image") or ""
            for variant_id, variant in article.get("variants", {}).items():
                nazov = variant.get("description", "")
                variant_image = variant.get("imageUrl") or variant.get("image") or article_image
                for bundle_id, bundle in variant.get("bundles", {}).items():
                    na_sklade = True
                    if bundle.get("availability") != "AVAILABLE": 
                        na_sklade = False
                    
                    sku = bundle.get("bettyBundleId", "")
                    url = f"https://sortiment.metro.sk/shop/pv/{sku}"
                    bundle_image = bundle.get("imageUrl") or bundle.get("image") or variant_image
                    if bundle_image and not bundle_image.startswith("http"):
                        bundle_image = "https://sortiment.metro.sk" + ("" if bundle_image.startswith("/") else "/") + bundle_image

                    for store_id, store in bundle.get("stores", {}).items():
                        cena_balenia = store.get("sellingPriceInfo", {}).get("finalPrice", 0.0)
                        cena_za_kg = store.get("basePriceData", {}).get("pricePerUnit", {}).get("grossPrice", 0.0)
                        
                        vaha_kg_regex, pocet_kusov = self._extract_weight_and_pieces(nazov)
                        
                        vaha_kg_kalkulacka = (cena_balenia / cena_za_kg) if cena_za_kg > 0 else 0
                        vaha_kg = vaha_kg_kalkulacka if vaha_kg_kalkulacka > 0 else vaha_kg_regex
                        
                        if vaha_kg <= 0: 
                            continue

                        normalizovane.append({
                            "sku": sku, 
                            "name": nazov, 
                            "cena_balenia": round(cena_balenia, 2),
                            "cena_za_kg": round(cena_za_kg, 2), 
                            "vaha_kg": round(vaha_kg, 3), 
                            "url": url,
                            "image_url": bundle_image, 
                            "in_stock": na_sklade,
                            "kusov_v_baleni": pocet_kusov,      
                            "zdroj_api": "JSONb (Nové API Metro)"     
                        })
        return normalizovane
    
    def _extract_weight_and_pieces(self, nazov: str) -> tuple:
        """Regex detektor hmotnosti, objemu a multipackov (napr. '6x1.5l' alebo '250 g')."""
        match_kombinacia = re.search(r'(\d+)\s*[xX]\s*(\d+(?:[.,]\d+)?)\s*(g|kg|l|ml)', nazov, re.IGNORECASE)
        match_samostatne = re.search(r'(\d+(?:[.,]\d+)?)\s*(g|kg|l|ml)', nazov, re.IGNORECASE)
        
        if match_kombinacia:
            pocet = int(match_kombinacia.group(1)) 
            vaha = float(match_kombinacia.group(2).replace(',', '.'))
            jednotka = match_kombinacia.group(3).lower()
            vaha_kg = (pocet * vaha) / 1000 if jednotka in ['g', 'ml'] else (pocet * vaha)
            return vaha_kg, pocet
            
        elif match_samostatne:
            vaha = float(match_samostatne.group(1).replace(',', '.'))
            jednotka = match_samostatne.group(2).lower()
            vaha_kg = vaha / 1000 if jednotka in ['g', 'ml'] else vaha
            return vaha_kg, 1 
            
        return 0.0, 1


# ==========================================
# 5. PRACOVNÁ LOGIKA, FILTRÁCIA A ARCHIVÁCIA
# ==========================================
class ShoppingWorker:
    def __init__(self, providers: dict):
        """Inicializácia s mapou providerov, napr. {'metro': MetroProvider()}"""
        self.providers = providers
        self.queue = asyncio.Queue()
        self.known_tasks = set()

    async def enqueue_task(self, task: dict):
        task_id = task["id"]
        if task_id not in self.known_tasks:
            self.known_tasks.add(task_id)
            await self.queue.put(task)
            print(f"📥 [FRONTA] Úloha {task_id[:8]}... pridaná do radu.")

    async def process_task(self, task: dict):
        task_id = task["id"]
        self.known_tasks.discard(task_id)
        
        produkt_info = task.get("abstract_products") or {}
        display_nazov = produkt_info.get("name", "Neznámy produkt")
        
        global_banned = produkt_info.get("global_banned_keywords") or []
        global_required = produkt_info.get("global_required_keywords") or []
        
        mappings = produkt_info.get("store_mappings") or []
        store_map = mappings[0] if mappings else {} 
        
        # Určenie providera na základe DB konfigurácie (default je 'metro')
        provider_key = store_map.get("provider", "metro").lower()
        provider: BaseStoreProvider = self.providers.get(provider_key)
        
        if not provider:
            print(f"❌ Neznámy provider '{provider_key}' pre úlohu {task_id}. Preskakujem.")
            await self._update_status(task_id, "failed")
            return

        vyhladavaci_nazov = store_map.get("search_query") or display_nazov
        locked_sku_id = store_map.get("exact_sku_locked")
        
        store_banned = store_map.get("store_banned_keywords") or []
        store_required = store_map.get("store_required_keywords") or []
        
        vsetky_banned = global_banned + store_banned
        vsetky_required = global_required + store_required

        min_vaha = task.get("min_tolerance_kg")
        if min_vaha is None: min_vaha = produkt_info.get("default_min_kg", 0.05)
            
        max_vaha = task.get("max_tolerance_kg")
        if max_vaha is None: max_vaha = produkt_info.get("default_max_kg", 100.0)
            
        povoleny_karton = task.get("allow_multipack", False)
        pozadovane_mnozstvo = task.get("quantity") or 1

        print(f"\n" + "="*60)
        print(f"🛒 SPRACOVÁVAM [{provider_key.upper()}]: '{display_nazov}'")
        await self._update_status(task_id, "processing")

        try:
            # ---------------------------------------------------------
            # REŽIM: HARD LOCK (Priamy nákup konkrétneho SKU)
            # ---------------------------------------------------------
            if locked_sku_id:
                print(f"🔒 REŽIM: Hard Lock (Priame vloženie do košíka)")
                sku_odpoved = await supabase.table("sku_variants").select("sku").eq("id", locked_sku_id).execute()
                if not sku_odpoved.data:
                    raise Exception("Nenašlo sa priradené SKU v databáze.")
                
                finalne_sku = sku_odpoved.data[0]["sku"]
                
                try:
                    await supabase.table("analyzed_products_archive").insert({
                        "task_id": task_id,
                        "search_query": vyhladavaci_nazov,
                        "sku": finalne_sku,
                        "name": display_nazov,
                        "filter_status": "hard_lock",
                        "rejection_reason": "Produkt bol v databáze natvrdo uzamknutý na toto SKU."
                    }).execute()
                except Exception as arch_err:
                    print(f"[ARCHÍV] ⚠️ Nepodarilo sa zapísať Hard Lock do histórie: {arch_err}")

                if await provider.add_to_cart(finalne_sku, quantity=pozadovane_mnozstvo):
                    await self._update_status(task_id, "bought", finalne_sku)
                    print(f"✅ Vybavené! (SKU: {finalne_sku} pridané do košíka. Množstvo: {pozadovane_mnozstvo})")
                else:
                    raise Exception("Zlyhalo API pridanie do košíka.")
            
            # ---------------------------------------------------------
            # REŽIM: LOV NA NAJLEPŠIU CENU (Filtrovanie a výber)
            # ---------------------------------------------------------
            else:
                await provider.log_event("info", f"🔎 [RÖNTGEN] Lov na najlepšiu cenu pre: '{display_nazov}'")
                
                najdene_produkty = await provider.search_product(vyhladavaci_nazov)
                unikatne_produkty = list({p['sku']: p for p in najdene_produkty}.values())

                await provider.log_event("info", f"🔬 [RÖNTGEN] Skenujem a filtrujem {len(unikatne_produkty)} produktov...")
                
                odfiltrovane = []
                archivne_zaznamy = []
                statistika = {"passed": 0, "failed": 0}
                
                for p in unikatne_produkty:
                    status = "passed"
                    reason = ""
                    zdroj = p.get("zdroj_api", "Neznámy zdroj")
                    je_karton = p.get("kusov_v_baleni", 1) > 1
                    
                    vyskyt_zakazaneho = [zak for zak in vsetky_banned if zak.lower() in p["name"].lower()]
                    
                    splnene_povinne = [pov for pov in vsetky_required if pov.lower() in p["name"].lower()]

                    if not p.get("in_stock", True):
                        status = "out_of_stock"
                        reason = "Vypredané na pobočke."
                        logger.warning(f"    ❌ [{zdroj}] '{p['name']}' -> {reason}")
                        statistika["failed"] += 1
                        
                    elif vyskyt_zakazaneho:
                        status = "banned_keyword"
                        reason = f"Blacklist slovo: '{vyskyt_zakazaneho[0]}'"
                        logger.warning(f"    ❌ [{zdroj}] '{p['name']}' -> {reason}")
                        statistika["failed"] += 1
                    
                    elif vsetky_required and not splnene_povinne:
                        status = "missing_required_keyword"
                        reason = f"Nenašlo sa ani jedno Whitelist slovo z požadovaných: {vsetky_required}"
                        logger.warning(f"    ❌ [{zdroj}] '{p['name']}' -> {reason}")
                        statistika["failed"] += 1
                        
                   
                        
                    elif p["vaha_kg"] < min_vaha:
                        status = "underweight"
                        reason = f"Podlimitná váha ({p['vaha_kg']:.3f} kg < limit {min_vaha} kg)"
                        logger.warning(f"    ❌ [{zdroj}] '{p['name']}' -> {reason}")
                        statistika["failed"] += 1
                    
                    elif je_karton:
                        if not povoleny_karton:
                            status = "unwanted_multipack"
                            reason = f"Karton nie je povolený ({p['kusov_v_baleni']} ks)"
                            logger.warning(f"    ❌ [{zdroj}] '{p['name']}' -> {reason}")
                            statistika["failed"] += 1
                        else:
                            logger.success(f"    ✅ [{zdroj}] '{p['name']}' (KARTÓN: {p['kusov_v_baleni']}ks, Spolu {p['vaha_kg']:.2f}kg) -> VYHOVUJE")
                            odfiltrovane.append(p)
                            statistika["passed"] += 1
                            
                    elif p["vaha_kg"] > max_vaha:
                        status = "overweight"
                        reason = f"Nadlimitná váha ({p['vaha_kg']:.3f} kg > limit {max_vaha} kg)"
                        logger.warning(f"    ❌ [{zdroj}] '{p['name']}' -> {reason}")
                        statistika["failed"] += 1
                        
                    else:
                        logger.success(f"    ✅ [{zdroj}] '{p['name']}' (Spolu {p['vaha_kg']:.2f}kg) -> VYHOVUJE")
                        odfiltrovane.append(p)
                        statistika["passed"] += 1
                    
                    archivne_zaznamy.append({
                        "task_id": task_id, "search_query": vyhladavaci_nazov, "sku": p["sku"],
                        "name": p["name"], "cena_balenia": p["cena_balenia"], "cena_za_kg": p["cena_za_kg"],
                        "vaha_kg": p["vaha_kg"], "url": p["url"], "image_url": p.get("image_url", ""),
                        "filter_status": status, "rejection_reason": reason,
                        "api_source": zdroj
                    })

                if not odfiltrovane:
                    if archivne_zaznamy:
                        await supabase.table("analyzed_products_archive").insert(archivne_zaznamy).execute()
                    await provider.log_event("error", "❌ [RÖNTGEN] Zlyhanie: Všetky nájdené produkty boli odfiltrované.")
                    raise Exception("Po filtrácii nezostal žiadny vyhovujúci produkt.")

                # Určenie víťaza
                odfiltrovane.sort(key=lambda x: x["cena_za_kg"])
                vitaz = odfiltrovane[0]
                
                for zaznam in archivne_zaznamy:
                    if zaznam["sku"] == vitaz["sku"] and zaznam["filter_status"] == "passed":
                        zaznam["filter_status"] = "winner"

                try:
                    await supabase.table("analyzed_products_archive").insert(archivne_zaznamy).execute()
                    await provider.log_event("success", f"📦 [ARCHÍV] Uložených {len(archivne_zaznamy)} záznamov (Vyhovelo: {statistika['passed']}, Vyradené: {statistika['failed']}).")
                except Exception as e:
                    logger.error(f"[ARCHÍV] ⚠️ Nepodarilo sa zapísať históriu hľadania: {e}")

                await provider.log_event("success", f"🏆 VÍŤAZ: {vitaz['name']} ({vitaz['cena_balenia']}€ | {vitaz['cena_za_kg']}€/kg)")

                # Kalkulačka a vkladanie
                pocet_v_baleni_vitaza = vitaz.get("kusov_v_baleni", 1)
                finalne_mnozstvo_do_kosika = math.ceil(pozadovane_mnozstvo / pocet_v_baleni_vitaza)
                
                await provider.log_event("info", f"🧮 Prepočet: Objednávka {pozadovane_mnozstvo} ks. Víťaz má {pocet_v_baleni_vitaza} ks/bal. Do košíka ide: {finalne_mnozstvo_do_kosika} ks.")

                if await provider.add_to_cart(vitaz["sku"], quantity=finalne_mnozstvo_do_kosika):
                    bezpecny_link = vitaz.get("url") or f"https://sortiment.metro.sk/shop/pv/{vitaz['sku']}"
                    await supabase.table("shopping_queue").update({
                        "status": "bought",
                        "product_url": bezpecny_link,
                        "product_image_url": vitaz.get("image_url") 
                    }).eq("id", task_id).execute()
                    await provider.log_event("success", f"✅ Úloha '{display_nazov}' úspešne vybavená a zapísaná v systéme!")
                else:
                    await provider.log_event("error", "❌ Zlyhalo pridanie víťaza do API košíka Metra.")
                    raise Exception("Zlyhalo API pridanie víťaza do košíka.")
        except Exception as e:
            print(f"❌ Nastala kritická chyba pri spracovaní tohto produktu: {e}")
            await self._update_status(task_id, "failed")

    async def _update_status(self, task_id: str, status: str, sku: str = None):
        data = {"status": status}
        if sku: 
            data["purchased_sku"] = sku
        try:
            await supabase.table("shopping_queue").update(data).eq("id", task_id).execute()
        except Exception as e:
            print(f"[SUPABASE] ⚠️ Chyba pri aktualizácii statusu: {e}")

    async def worker_loop(self):
        while True:
            task = await self.queue.get()
            await self.process_task(task)
            self.queue.task_done()


# ==========================================
# 6. REALTIME SUPABASE LISTENER (WebSocket)
# ==========================================
async def setup_realtime_listener(worker: ShoppingWorker):
    def on_new_insert(payload):
        new_record = payload.get("record", {})
        if new_record.get("status") == "pending":
            asyncio.create_task(fetch_and_queue(new_record["id"], worker))

    async def fetch_and_queue(task_id, worker):
        try:
            detail = await supabase.table("shopping_queue").select("*, abstract_products(*, store_mappings(*))").eq("id", task_id).execute()
            if detail.data:
                await worker.queue.put(detail.data[0])
        except Exception as e:
            print(f"[REALTIME] ⚠️ Chyba pri dopytovaní novej úlohy: {e}")

    print("[REALTIME] 📡 Pripájam sa na WebSocket kanál Supabase...")
    try:
        channel = supabase.channel("room-shopping-queue")
        channel.on_postgres_changes(
            event="*",
            schema="public",
            table="shopping_queue",
            callback=on_new_insert
        )
        await channel.subscribe()
        print("[REALTIME] ✅ WebSocket úspešne spojený a aktívny!")
    except Exception as e:
        print(f"[REALTIME] ❌ Nepodarilo sa pripojiť na WebSocket: {e}")


async def background_poller(worker: ShoppingWorker):
    print("[POLLER] 🧹 Záchranný zametač aktivovaný. Kontroluje databázu každých 60 sekúnd...")
    while True:
        await asyncio.sleep(60)
        try:
            pending_tasks = await supabase.table("shopping_queue").select("*, abstract_products(*, store_mappings(*))").eq("status", "pending").execute()
            if pending_tasks.data:
                for task in pending_tasks.data:
                    await worker.enqueue_task(task)
        except Exception as e:
            pass


# ==========================================
# 7. ŠTART APLIKÁCIE
# ==========================================
async def main():
    global supabase
    supabase = await create_async_client(SUPABASE_URL, SUPABASE_KEY)
    
    # NOVÉ: Inicializujeme náš centrálny SystemLogger pre UI toasty
    app_logger = SystemLogger(supabase_client)
    # 1. Inicializácia všetkých dostupných providerov, teraz posielame logger
    metro_provider = MetroProvider(logger_instance=app_logger)
    await metro_provider.initialize()
    
    # Príprava na multiobchodnú sieť (Kľúč v slovníku zodpovedá stĺpcu v DB)
    active_providers = {
        "metro": metro_provider
        # "tesco": TescoProvider()  <- Sem v budúcnosti pridáte len jeden riadok!
    }

    # 2. Inicializácia workera so zoznamom providerov
    worker = ShoppingWorker(providers=active_providers)
    
    # 3. Načítanie úvodných pending úloh
    pending_tasks = await supabase.table("shopping_queue").select("*, abstract_products(*, store_mappings(*))").eq("status", "pending").execute()
    for task in pending_tasks.data:
        await worker.queue.put(task)
    
    await setup_realtime_listener(worker)
    
    print("\n[SYSTEM] 🚀 Nákupný Worker úspešne spustený v Multi-Provider režime. Čakám na objednávky...")
    asyncio.create_task(background_poller(worker))
    await worker.worker_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SYSTEM] 🛑 Worker bol manuálne ukončený.")