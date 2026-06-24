import os
from dotenv import load_dotenv
from supabase import create_client

# 1. Načítanie kľúčov
load_dotenv(override=True)
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")

if not url or not key:
    print("❌ Chýbajú kľúče v .env")
    exit(1)

supabase = create_client(url, key)

def run_stress_test(pocet_uloh=10):
    print(f"🔍 Sťahujem vzorovú úlohu zo Supabase...")
    
    # Zoberieme prvú úlohu, ktorú už máš v databáze
    response = supabase.table("shopping_queue").select("*").limit(1).execute()
    
    if not response.data:
        print("❌ V tabuľke shopping_queue nie je žiadny vzorový záznam. Pridaj aspoň jeden cez UI.")
        return

    vzor = response.data[0]
    nove_ulohy = []
    
    for i in range(pocet_uloh):
        nova_uloha = vzor.copy()
        
        # Odstránime unikátne systémové polia, o ktoré sa postará Supabase sama
        if "id" in nova_uloha: del nova_uloha["id"]
        if "created_at" in nova_uloha: del nova_uloha["created_at"]
        if "updated_at" in nova_uloha: del nova_uloha["updated_at"]
        
        # Zresetujeme status na čakajúci a nastavíme náhodné množstvo pre zaujímavosť
        nova_uloha["status"] = "pending"
        nova_uloha["quantity"] = (i % 5) + 1  
        
        nove_ulohy.append(nova_uloha)
        
    print(f"📥 Vkladám {pocet_uloh} nových 'pending' úloh do databázy...")
    
    # Pre istotu vkladáme po menších dávkach (chunks), keby Supabase mala limit
    chunk_size = 25
    for i in range(0, len(nove_ulohy), chunk_size):
        chunk = nove_ulohy[i:i + chunk_size]
        supabase.table("shopping_queue").insert(chunk).execute()
        
    print(f"✅ Hotovo! V databáze ťa čaká {pocet_uloh} úloh. Môžeš naštartovať workera.")

if __name__ == "__main__":
    run_stress_test()