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
import httpx
# ==========================================
# 1. KONFIGURÁCIA SUPABASE
# ==========================================
# ⬅️ NOVÉ: Načítanie tajných kľúčov zo súboru .env
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# ⬅️ NOVÉ: Fail-fast ochrana. Ak skript nenájde kľúče v .env, hneď sa vypne, aby nevyhadzoval nezmyselné chyby neskôr.
if not SUPABASE_URL or not SUPABASE_KEY:
    print("CRITICAL ERROR: Chýbajú Supabase kľúče v .env súbore!", file=sys.stderr)
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ==========================================
# 2. ZÁKLADNÉ ROZHRANIE PRE PROVIDEROV (Provider Interface)
# ==========================================
class BaseStoreProvider(ABC):
    """
    Abstraktná trieda, ktorú musí implementovať každý e-shop (Metro, Tesco, atď.).
    ShoppingWorker vďaka tomu komunikuje s akýmkoľvek obchodom rovnakým spôsobom.
    """
    
    @abstractmethod
    async def initialize(self) -> None:
        """Inicializuje HTTP klienta, načíta cookies / session tokeny."""
        pass

    @abstractmethod
    async def search_product(self, query: str) -> list:
        """Vyhľadá produkt a vráti jednotný zoznam znormalizovaných slovníkov."""
        pass

    @abstractmethod
    async def add_to_cart(self, sku: str, quantity: int) -> bool:
        """Pridá položku do košíka a vráti True/False podľa úspešnosti."""
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
    def __init__(self):
        self.store_id = "00021"
        self.customer_id = "7031100021163708"
        self.api_betty_url = "https://sortiment.metro.sk/searchdiscover/articlesearch/search"
        self.api_legacy_url = "https://www.metro.sk/metro/UnifiedSearch/Search"
        self.api_cart_url = "https://api.metro.sk/v1/cart/add" 
        self.client = None

    async def initialize(self):
        cookies = await get_metro_cookies()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "sk-SK,sk;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.metro.sk/",
            "Origin": "https://www.metro.sk",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if self.client:
            await self.client.aclose()
        self.client = httpx.AsyncClient(cookies=cookies, headers=headers, timeout=15.0)

    async def search_product(self, query: str, is_retry: bool = False) -> list:
        if not self.client: 
            return []
        
        try:
            if not is_retry:
                await self.client.get("https://www.metro.sk/", headers={"Accept": "text/html"})
                await asyncio.sleep(random.uniform(0.5, 1.5))

            bezpecna_url = urllib.parse.quote(query)
            timestamp = int(time.time() * 1000)

            betty_url = (
                f"{self.api_betty_url}?"
                f"storeId={self.store_id}&"
                f"language=sk-SK&"
                f"country=SK&"
                f"query={bezpecna_url}&"
                f"rows=24&"
                f"page=1&"
                f"facets=true&"
                f"categories=true&"
                f"customerId={self.customer_id}&"
                f"__t={timestamp}"
            )
            
            print(f"[API] Odošlem dopyt na rozšírené API Metra (KROK 1): '{query}'")
            response = await self.client.get(betty_url)
            
            if response.status_code in [401, 403, 429] or "idam.metro.sk" in str(response.url):
                if is_retry: 
                    return []
                print("⚠️ Detekovaná expirovaná session! Spúšťam záchranné kolo...")
                await self.initialize()
                return await self.search_product(query, is_retry=True)
            
            produkty = []
            
            if response.status_code == 200:
                json_data = response.json()
                result_ids = json_data.get("resultIds", [])
                
                if result_ids:
                    print(f"📡 [JSONb KROK 1] Našiel som {len(result_ids)} kódov. Idem stiahnuť ich detailné ceny...")
                    zoznam_id_param = "&".join([f"ids={kod}" for kod in result_ids[:24]])
                    articles_url = (
                        f"https://sortiment.metro.sk/evaluate.article.v1/betty-variants?"
                        f"storeIds={self.store_id}&"
                        f"{zoznam_id_param}&"
                        f"country=SK&"
                        f"locale=sk-SK&"
                        f"customerId={self.customer_id}&"
                        f"__t={timestamp}"
                    )
                    
                    hlavicky_krok2 = {
                        "Accept": "application/json, text/plain, */*",
                        "Origin": "https://www.metro.sk",
                        "Referer": "https://www.metro.sk/",
                        "Sec-Fetch-Site": "same-site",
                        "Sec-Fetch-Mode": "cors"
                    }
                    
                    odpoved_ceny = await self.client.get(articles_url, headers=hlavicky_krok2)
                    
                    if odpoved_ceny.status_code == 200:
                        json_data = odpoved_ceny.json()
                        with open(f"DEBUG_NOVE_API_{query}.json", "w", encoding="utf-8") as f:
                            json.dump(json_data, f, indent=4, ensure_ascii=False)
                            
                        print(f"💾 [DEBUG] Surový JSONb uložený do: DEBUG_NOVE_API_{query}.json")
                        print("📡 [JSONb KROK 2] Ceny úspešne stiahnuté z nového API!")
                        produkty = self._normalize_json(json_data)
                    else:
                        print(f"⚠️ [DEBUG KROK 2] Zlyhalo! Status kód: {odpoved_ceny.status_code}")

            if not produkty:
                print("🔍 Rozšírené API nevrátilo kompletné dáta. Skúšam záložné zjednodušené API (JSONa)...")
                legacy_url = f"{self.api_legacy_url}?searchType=mshop&storeId={self.store_id}&searchTerm={bezpecna_url}"
                response_legacy = await self.client.get(legacy_url, headers={"X-Requested-With": "XMLHttpRequest"})
                
                if response_legacy.status_code == 200:
                    json_data = response_legacy.json()
                    with open(f"DEBUG_STARE_API_{query}.json", "w", encoding="utf-8") as f:
                        json.dump(json_data, f, indent=4, ensure_ascii=False)
                        
                    print(f"💾 [DEBUG] Surový JSONa uložený do: DEBUG_STARE_API_{query}.json")
                    produkty = self._normalize_json(json_data)

            return produkty

        except Exception as e:
            print(f"[METRO] ❌ Výnimka pri hľadaní '{query}': {e}")
            return []
    
    async def add_to_cart(self, sku: str, quantity: int = 1, is_retry: bool = False) -> bool:
        if "api.metro.sk" in self.api_cart_url:
            print(f"🛒 [SIMULÁCIA KOŠÍKA METRO] SKU {sku} (Množstvo: {quantity}) pridané do košíka.")
            return True

        try:
            payload = {"sku": sku, "quantity": quantity}
            response = await self.client.post(self.api_cart_url, json=payload)
            
            if response.status_code in [401, 403] or "idam.metro.sk" in str(response.url):
                if is_retry: 
                    return False
                print("⚠️ Expirovaná session pri vkladaní do košíka! Spúšťam záchranné kolo...")
                await self.initialize()
                return await self.add_to_cart(sku, quantity, is_retry=True)

            return response.status_code in [200, 201]
        except Exception as e:
            print(f"[METRO] ❌ Výnimka pri pridávaní do košíka SKU {sku}: {e}")
            return False

    def _normalize_json(self, json_data: dict) -> list:
        if "Results" in json_data:
            return self._spracuj_format_a(json_data["Results"])
        elif "results" in json_data:
            return self._spracuj_format_b(json_data["results"])
        elif "result" in json_data:
            return self._spracuj_format_b(json_data["result"])
        elif "articles" in json_data:
            return self._spracuj_format_b(json_data["articles"])
        else:
            print(f"⚠️ [DEBUG] API neposlalo produkty. Dostupné kľúče: {list(json_data.keys())}")
            return []

    def _spracuj_format_a(self, results_list: list) -> list:
        normalizovane = []
        for item in results_list:
            if not item: continue 
            
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
            
            try: 
                cena_balenia = float(re.sub(r'[^\d.,]', '', cena_str).replace(",", "."))
            except ValueError: 
                continue

            vaha_kg, pocet_kusov = self._extract_weight_and_pieces(nazov)
            if vaha_kg <= 0: 
                continue
            
            normalizovane.append({
                "sku": sku, "name": nazov, "cena_balenia": round(cena_balenia, 2),
                "cena_za_kg": round(cena_balenia / vaha_kg, 2) if vaha_kg > 0 else 0.0,
                "vaha_kg": vaha_kg, "url": url, "image_url": img_url, "in_stock": na_sklade,
                "kusov_v_baleni": pocet_kusov,
                "zdroj_api": "JSONa (Staré API Metro)"
            })
        return normalizovane

    def _spracuj_format_b(self, result_dict: dict) -> list:
        normalizovane = []
        for article_id, article in result_dict.items():
            article_image = article.get("imageUrl") or article.get("image") or ""
            for variant_id, variant in article.get("variants", {}).items():
                nazov = variant.get("description", "")
                variant_image = variant.get("imageUrl") or variant.get("image") or article_image
                for bundle_id, bundle in variant.get("bundles", {}).items():
                    na_sklade = True
                    if bundle.get("availability") != "AVAILABLE": na_sklade = False
                    
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
                        
                        if vaha_kg <= 0: continue

                        normalizovane.append({
                            "sku": sku, "name": nazov, "cena_balenia": round(cena_balenia, 2),
                            "cena_za_kg": round(cena_za_kg, 2), "vaha_kg": round(vaha_kg, 3), "url": url,
                            "image_url": bundle_image, "in_stock": na_sklade,
                            "kusov_v_baleni": pocet_kusov,      
                            "zdroj_api": "JSONb (Nové API Metro)"     
                        })
        return normalizovane
    
    def _extract_weight_and_pieces(self, nazov: str) -> tuple:
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
                print(f"🔎 REŽIM: Lov na najlepšiu cenu")
                
                najdene_produkty = await provider.search_product(vyhladavaci_nazov)
                unikatne_produkty = list({p['sku']: p for p in najdene_produkty}.values())

                print(f"  [RÖNTGEN] Filtrujem {len(unikatne_produkty)} nájdených produktov:")
                
                odfiltrovane = []
                archivne_zaznamy = []
                
                for p in unikatne_produkty:
                    status = "passed"
                    reason = ""
                    zdroj = p.get("zdroj_api", "Neznámy zdroj")
                    je_karton = p.get("kusov_v_baleni", 1) > 1
                    
                    vyskyt_zakazaneho = [zak for zak in vsetky_banned if zak.lower() in p["name"].lower()]
                    chybajuce_povinne = [pov for pov in vsetky_required if pov.lower() not in p["name"].lower()]
                    
                    if not p.get("in_stock", True):
                        status = "out_of_stock"
                        reason = "Produkt je momentálne vypredaný na pobočke."
                        print(f"    ❌ [{zdroj}] '{p['name']}' -> {reason}")
                        
                    elif vyskyt_zakazaneho:
                        status = "banned_keyword"
                        reason = f"Obsahuje zakázané slovo (Blacklist): '{vyskyt_zakazaneho[0]}'"
                        print(f"    ❌ [{zdroj}] '{p['name']}' -> {reason}")
                        
                    elif chybajuce_povinne:
                        status = "missing_required_keyword"
                        reason = f"Chýba povinné slovo (Whitelist): '{chybajuce_povinne[0]}'"
                        print(f"    ❌ [{zdroj}] '{p['name']}' -> {reason}")
                        
                    elif p["vaha_kg"] < min_vaha:
                        status = "underweight"
                        reason = f"Podlimitná celková váha ({p['vaha_kg']:.3f} kg < limit {min_vaha} kg)"
                        print(f"    ❌ [{zdroj}] '{p['name']}' -> {reason}")
                    
                    elif je_karton:
                        if not povoleny_karton:
                            status = "unwanted_multipack"
                            reason = f"Zamietnuté: Ide o kartón ({p['kusov_v_baleni']} ks) a kartóny nie sú povolené."
                            print(f"    ❌ [{zdroj}] '{p['name']}' -> {reason}")
                        else:
                            print(f"    ✅ [{zdroj}] '{p['name']}' (KARTÓN: {p['kusov_v_baleni']}ks, Spolu {p['vaha_kg']:.2f}kg) -> VYHOVUJE")
                            odfiltrovane.append(p)
                            
                    elif p["vaha_kg"] > max_vaha:
                        status = "overweight"
                        reason = f"Nadlimitná váha jedného kusu ({p['vaha_kg']:.3f} kg > limit {max_vaha} kg)"
                        print(f"    ❌ [{zdroj}] '{p['name']}' -> {reason}")
                        
                    else:
                        print(f"    ✅ [{zdroj}] '{p['name']}' (Spolu {p['vaha_kg']:.2f}kg) -> VYHOVUJE")
                        odfiltrovane.append(p)
                    
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
                    raise Exception("Po filtrácii nezostal žiadny vyhovujúci produkt.")

                odfiltrovane.sort(key=lambda x: x["cena_za_kg"])
                vitaz = odfiltrovane[0]
                
                for zaznam in archivne_zaznamy:
                    if zaznam["sku"] == vitaz["sku"] and zaznam["filter_status"] == "passed":
                        zaznam["filter_status"] = "winner"

                try:
                    await supabase.table("analyzed_products_archive").insert(archivne_zaznamy).execute()
                    print(f"📦 [ARCHÍV] Úspešne archivovaných {len(archivne_zaznamy)} produktov.")
                except Exception as e:
                    print(f"[ARCHÍV] ⚠️ Nepodarilo sa zapísať históriu hľadania: {e}")

                print(f"🏆 Víťaz: {vitaz['name']} ({vitaz['cena_balenia']}€ | {vitaz['cena_za_kg']}€/kg)")

                pocet_v_baleni_vitaza = vitaz.get("kusov_v_baleni", 1)
                finalne_mnozstvo_do_kosika = math.ceil(pozadovane_mnozstvo / pocet_v_baleni_vitaza)
                
                print(f"🧮 Prepočet: Chcel si {pozadovane_mnozstvo} ks. Víťaz má {pocet_v_baleni_vitaza} ks/balenie.")
                print(f"🛒 Do košíka API vkladám množstvo: {finalne_mnozstvo_do_kosika}")

                if await provider.add_to_cart(vitaz["sku"], quantity=finalne_mnozstvo_do_kosika):
                    bezpecny_link = vitaz.get("url") or f"https://sortiment.metro.sk/shop/pv/{vitaz['sku']}"
                    await supabase.table("shopping_queue").update({
                        "status": "bought",
                        "product_url": bezpecny_link,
                        "product_image_url": vitaz.get("image_url") 
                    }).eq("id", task_id).execute()
                    print(f"✅ Vybavené a zapísané v Supabase.")
                else:
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

    # 1. Inicializácia všetkých dostupných providerov
    metro_provider = MetroProvider()
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