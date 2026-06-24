Aby si mal v rámci projektu poriadok a tvoj GitHub repozitár pôsobil profesionálne (a vyhýbal sa zbytočným blokáciám), odporúčam pridať do tvojho "taháka" (inštrukcií) tieto body.

Tu je upravený návrh, ktorý si môžeš vložiť do svojich poznámok k projektu:

---

### 📋 Inštrukcie pre Git & GitHub (Workflow)

#### 1. Bezpečnosť (GitHub Secret Scanning)

* **Nikdy** neukladaj reálne kľúče (Supabase, API kľúče) priamo do súborov v Gite.
* **Vždy** používaj súbor `.env` a pridaj ho do `.gitignore`.
* Ak ti GitHub zablokuje push:
1. Skontroluj súbory uvedené v chybe.
2. Ak si tam omylom pushol kľúč, zmaž ho a urob `git reset --soft HEAD~1`.
3. Ak je to tvoj testovací kľúč, povoľ ho na linke, ktorú ti GitHub vygeneroval (tzv. *Unblock secret*).



#### 2. Verziovanie a Tagovanie (Pre Workera)

Používame systém **"Conventional Tags"** s prefixom, aby sa nám v repozitári nepomiešali verzie (napr. Worker vs. Frontend).

* **Vytvorenie tagu:**
```bash
git tag -a worker-v0.0.1 -m "Verzia workera 0.0.1 - popis hlavnej zmeny"

```


* **Odoslanie tagu na GitHub:**
```bash
git push origin worker-v0.0.1

```


* **Zobrazenie všetkých verzií:**
```bash
git tag

```



#### 3. Čistý pracovný postup (Best Practices)

* **Pred každým commitom:** Skontroluj, čo všetko pridávaš: `git status`.
* **Konvenčné správy commitov:** Používaj formát `typ(scope): správa` (napr. `feat(worker): pridanie logiky pre Metro`, `fix(auth): oprava 403 chyby`).
* **Pravidlo jednej zložky:** Ak robíš zmeny len vo workeri, pridávaj len túto zložku: `git add worker/`.

#### 4. Kedy vytvoriť nový Tag?

Tag vytváraj vždy, keď:

1. Worker úspešne prešiel testami a vie spracovať nákup.
2. Pridal si novú podporu pre ďalší obchod (napr. Tesco).
3. Urobil si dôležitú zmenu v `BaseStoreProvider`, ktorá mení spôsob komunikácie.

---

### Prečo tieto inštrukcie?

* **Prefix `worker-**`: Umožňuje ti neskôr pridať napríklad `frontend-v0.1.0` bez toho, aby si mal v zozname chaos.
* **Secret Scanning:** Tieto body ťa chránia pred tým, aby si musel znova prechádzať tým "pekielkom" s blokovaním push-ov, ktoré sme si dnes zažili.
* **Stav `git status**`: Toto je tvoj najlepší priateľ. Pred každým `add` vidíš, čo ideš odoslať, takže sa vyhneš tomu, aby si tam omylom pribalil niečo, čo tam nepatrí.

