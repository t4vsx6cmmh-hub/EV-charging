# Elopladning til biler

Se præcist hvad elbilen koster at lade – adskilt fra husets almindelige forbrug – beregnet med de rigtige variable elpriser kvarter for kvarter.

Appen kører på en computer derhjemme (fx en Raspberry Pi, en gammel PC eller en Mac), og du åbner dashboardet i browseren på telefonen eller computeren.

![Dashboard](docs/dashboard.png)

## Hvad den gør

| Data | Kilde |
|---|---|
| Elbilens opladning, live og præcist | **Tesla Wall Connector (Gen 3)** – lokalt API på hjemmenetværket |
| Husstandens samlede forbrug pr. kvarter | **Eloverblik.dk** (Energinet DataHub) |
| Nettarif, Energinets tariffer og elafgift for netop din adresse | **Eloverblik.dk** |
| Spotpriser (DK1/DK2) pr. kvarter, også for i morgen | **Energi Data Service** (Energinet) |
| Elselskabets tillæg (Andel Energi) | Du skriver det selv i `config.json` |

**Husets forbrug = elmålerens forbrug − Wall Connectorens forbrug**, beregnet for hvert kvarter. Prisen pr. kWh i et kvarter er:

```
(spotpris + Andel Energi-tillæg + nettarif + Energinet-tariffer + elafgift) × 1,25 moms
```

Appen har fire faner:

- **Nu** – bilens batteri, energiflowet fra elnettet til hus og bil, elprisen lige nu og månedens tal
- **Plan** – smart opladning: træk i tidslinjen for at vælge "klar kl.", og vælg mål med skyderen
- **Priser** – elprisen som en vejrudsigt for i går, i dag og i morgen, og hvad prisen består af
- **Historik** – besparelser, elbil mod benzin, forbrug pr. dag/måned og alle ladesessioner

Den viser blandt andet:

- elprisen lige nu og priskurven for i dag og i morgen
- om bilen lader, med hvor mange kW, og hvad sessionen koster
- det billigste tidspunkt at lade i de næste døgn
- kroner og kWh for elbil og hus pr. dag og pr. måned
- hver ladesession med pris og hvad den *kunne* have kostet, hvis den var ladet i de billigste kvarterer
- **smart opladning**: en plan for de billigste kvarterer inden fx kl. 07 – og med Tesla Fleet API starter og stopper appen selv opladningen
- **notifikationer** på telefonen, når opladningen stopper, når morgendagens priser kommer, og hvis noget holder op med at virke
- **app på telefonen**: installér dashboardet på hjemmeskærmen – det åbner i fuld skærm og viser seneste tal, også uden forbindelse
- **CSV-eksport** pr. dag, måned og ladesession (fx til refusion fra arbejdsgiver) og **faste abonnementer** med i månedsregningen

For perioder **før** appen blev startet (hvor Wall Connectoren ikke er logget), skønner appen elbilens andel ud fra elmåleren: længere perioder med over 5 kW forbrug regnes som opladning. Disse tal er markeret med ≈.

## Kom i gang

### 1. Krav

- Python 3.9 eller nyere (ingen ekstra pakker – på Windows dog `pip install tzdata`)
- Computeren skal være på samme netværk som Wall Connectoren og stå tændt

### 2. Prøv demoen først

```bash
python3 -m elopladning --demo
```

Åbn http://localhost:8080. Demoen bruger opdigtede data.

### 3. Find Wall Connectorens IP-adresse

Se listen over tilsluttede enheder i din router (den hedder typisk noget med "TeslaWallConnector"). Test i browseren: `http://<ip>/api/1/vitals` skal vise en side med tal. Giv den gerne en fast IP-adresse i routeren.

### 4. Hent et token fra Eloverblik

1. Log ind på [eloverblik.dk](https://eloverblik.dk) med MitID.
2. Gå til **Datadeling** (eller "Del data") → **Opret token**, og kopier tokenet.

### 5. Find Andel Energis tillæg

Se din regning eller prisoversigten for dit produkt på Andel Energis hjemmeside. Tillægget står typisk i øre/kWh. Skriv det i kroner **uden moms** – fx 6 øre inkl. moms = `0.048`.

### 6. Udfyld config.json

```bash
cp config.example.json config.json
```

| Felt | Betydning |
|---|---|
| `wall_connector_host` | IP-adressen fra trin 3 |
| `eloverblik_refresh_token` | Tokenet fra trin 4 |
| `price_area` | `DK2` = Østdanmark (Sjælland, øerne og Bornholm), `DK1` = Jylland og Fyn |
| `supplier_addon_kr_per_kwh` | Tillæg fra trin 5, kr/kWh uden moms |
| `metering_point_id` | Tom = vælg automatisk dit forbrugsmålepunkt |
| `manual_tariffs` | Normalt `null`. Hvis Eloverblik ikke giver tariffer, kan de skrives ind: `{"net_hourly": [24 priser], "elafgift": 0.008, "systemtarif": 0.07}` |
| `history_days` | Hvor mange dages forbrug og priser der hentes første gang |
| `consumption_kwh_per_100km` | Bilens forbrug (fx 18) – bruges til km og kr/km |
| `petrol_l_per_100km` / `petrol_price_kr_per_l` | Den benzinbil, elbilen sammenlignes med (fx 6 l/100 km à 13,50 kr) |
| `subscriptions_kr_per_month` | Faste abonnementer pr. måned **inkl. moms**, som de står på regningen, fx `{"Netabonnement": 26.25, "Andel Energi abonnement": 29}` |

De øvrige felter (smart opladning, Tesla og notifikationer) er beskrevet nedenfor og kan stå tomme. Appen tjekker `config.json` ved start og fortæller præcist, hvilke felter der er forkerte.

### 7. Start

```bash
python3 -m elopladning
```

Åbn `http://<computerens-ip>:8080` på telefonen, mens den er på hjemmenetværket.

Appen spørger Wall Connectoren hvert 30. sekund og henter priser og forbrug én gang i timen. Data gemmes i `elopladning.db`.

### Start automatisk (Raspberry Pi / Linux)

```ini
# /etc/systemd/system/elopladning.service
[Unit]
Description=Elopladning til biler
After=network-online.target

[Service]
WorkingDirectory=/home/pi/elopladning-til-biler
ExecStart=/usr/bin/python3 -m elopladning
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now elopladning
```

## Smart opladning

Øverst i dashboardet vælger du:

- **Fra** – appen rører ikke bilen (planen vises stadig som inspiration).
- **Smart** – appen lægger en plan med de billigste kvarterer, så bilen er klar til det klokkeslæt, du vælger. Planen ses også som blå søjler på priskurven.
- **Lad nu** – lad med det samme.

Hvor meget der skal lades, beregnes ud fra bilens batteriniveau og dit mål i % (højst bilens ladegrænse), hvis Tesla Fleet API er sat op (se nedenfor). Ellers skriver du selv, hvor mange kWh der skal lades i alt. Morgendagens priser offentliggøres ca. kl. 13; indtil da skønnes de ud fra dagens priser (markeret med ≈), og planen rettes til, når de rigtige priser kommer.

| Felt | Betydning |
|---|---|
| `charge_kw` | Ladeeffekt (11 kW for 3×16 A) |
| `battery_kwh` | Batteriets brugbare størrelse, fx 75 (Model Y Long Range) |
| `charge_efficiency` | Hvor stor en del af strømmen der ender i batteriet (typisk 0,9) |
| `smart_default_ready` / `smart_default_kwh` | Udgangspunkt for "klar kl." og kWh – kan ændres i dashboardet |

**Uden Tesla Fleet API** er planen en anbefaling: sæt fx "planlagt opladning" i Tesla-appen til starttidspunktet. Wall Connectorens lokale API kan kun læse data, ikke starte/stoppe.

### Lad appen styre bilen (Tesla Fleet API)

1. Opret en app på [developer.tesla.com](https://developer.tesla.com) med scopes *Vehicle Information* og *Vehicle Charging Management*. Angiv en redirect-adresse (fx `https://localhost/callback` – siden behøver ikke findes). Følg Teslas vejledning for at registrere appen ("partner account" med en offentlig nøgle på dit domæne).
2. Skriv `tesla_client_id`, `tesla_client_secret` og `tesla_redirect_uri` i `config.json` (`tesla_region`: `eu` i Danmark).
3. Kør `python3 -m elopladning --tesla-login`, åbn adressen, log ind og indsæt adressen, du ender på. Adgangen gemmes i databasen og fornyes automatisk.
4. Nyere biler kræver **signerede kommandoer**: kør Teslas [`tesla-http-proxy`](https://github.com/teslamotors/vehicle-command) på samme computer, og sæt `tesla_command_proxy` til fx `https://localhost:4443` og `tesla_proxy_ca_file` til proxyens certifikat. Batteridata hentes stadig direkte.

Tesla tager betaling pr. kald til Fleet API (der er et gratis månedligt beløb). Appen holder kaldene nede: batteriet læses højst hver halve time, mens bilen er sat til, og en kommando gentages tidligst efter 10 minutter.

## Notifikationer

Appen sender beskeder via [ntfy](https://ntfy.sh) – en gratis app til iPhone og Android, uden konto.

1. Installer ntfy og abonnér på et hemmeligt emne, som andre ikke kan gætte, fx `elopladning-k8f2x9q`.
2. Skriv emnet i `ntfy_topic`. Tryk **Send testnotifikation** nederst i dashboardet.

| Felt | Besked |
|---|---|
| `notify_session_end` | Når opladningen stopper: kWh, pris og kr/kWh |
| `notify_smart_plan` | Når bilen sættes til i smart-tilstand: planen og hvad den sparer |
| `notify_tomorrow_prices` | Når morgendagens priser er ude: billigste `notify_block_hours` timer, laveste og højeste pris – og perioder under `notify_price_below` kr/kWh, hvis det er sat |
| `notify_errors` | Hvis Wall Connector, Eloverblik eller spotpriser har fejlet i over 2 timer (og når det virker igen) |

Kører du din egen ntfy-server, så sæt `ntfy_server` og evt. `ntfy_token`.

## Eksport og abonnementer

Knapperne **Eksportér CSV** henter tabellerne som CSV med semikolon og decimalkomma, så de åbner direkte i dansk Excel og Numbers. Med `subscriptions_kr_per_month` udfyldt viser tabellen også abonnementerne og hele regningen; pr. dag fordeles månedsbeløbet på månedens dage.

## Installér som app på telefonen

Dashboardet er en såkaldt web-app (PWA): den lægges på hjemmeskærmen med eget ikon og åbner i fuld skærm uden adresselinje – ingen App Store nødvendig.

**iPhone/iPad (Safari):** åbn `http://<computerens-ip>:8080`, tryk på **Del** (firkanten med pilen) → **Føj til hjemmeskærm** → **Tilføj**. Knappen **Installér app** i dashboardet viser samme vejledning.

**Android (Chrome):** tryk **Installér app** i dashboardet, eller menuen ⋮ → **Installér app**.

Fuld app-funktion – installation på Android og visning af seneste tal uden forbindelse – kræver, at dashboardet åbnes via **https**. Over almindelig http på hjemmenetværket virker genvejen på hjemmeskærmen stadig, men uden offline-visning (og på Android som en almindelig browser-genvej).

### https og adgang hjemmefra med Tailscale (anbefalet)

[Tailscale](https://tailscale.com) er gratis til privat brug og giver en sikker forbindelse til appen – også når du ikke er hjemme – uden at åbne noget mod internettet:

1. Installér Tailscale på computeren med appen og på telefonen, og log ind med samme konto.
2. På computeren: `tailscale serve --bg 8080`
3. Åbn den viste adresse (fx `https://pi.tail1234.ts.net`) på telefonen og installér appen derfra.
4. Skriv adressen i `config.json`: `"allowed_origins": ["https://pi.tail1234.ts.net"]`.

Har du selv et certifikat (fx fra `tailscale cert`), kan appen også køre https direkte med `tls_cert_file` og `tls_key_file`.

## Docker

```bash
mkdir data && cp config.example.json data/config.json   # udfyld data/config.json
docker compose up -d
```

Config og database ligger i `data/`. Brug `docker compose run --rm elopladning python -m elopladning --tesla-login` til Tesla-login.

## Godt at vide

- **Eloverblik halter 1–2 døgn.** Husets forbrug for i dag og i går vises som "afventer". Elbilens opladning ses med det samme.
- **Abonnementer og faste gebyrer** (net- og elselskabsabonnement) er ikke med. Appen fordeler kun det, der betales pr. kWh.
- **Tarifferne** hentes én gang i døgnet. Ændrer netselskabet tarif (fx skift mellem sommer- og vintertarif), bruges den nye pris også bagud i tid, indtil den er hentet. Det giver kun en lille afvigelse.
- **Wall Connectoren** måler lidt anderledes end elmåleren. Hvis den måler mere end elmåleren i et kvarter, skaleres elbilen ned til målerens tal.
- **Til familie og venner:** hver husstand kører sin egen kopi med egen Wall Connector og eget Eloverblik-token. Ingen data forlader jeres hjemmenetværk ud over kaldene til Energinet (og Tesla/ntfy, hvis de er slået til).
- **Sikkerhed:** dashboardet har intet login – alle på hjemmenetværket kan se det og skifte opladningstilstand. Gør det ikke tilgængeligt fra internettet (ingen port-forwarding); brug fx VPN til hjemmet. Med `"listen_host": "127.0.0.1"` kan det kun åbnes fra computeren selv.

## Test

```bash
python3 -m unittest discover tests
```

Testene kører automatisk på GitHub (Python 3.9–3.13, Linux og Windows) ved hvert push.

## Licens

Chart.js (MIT) og skrifttypen Inter (SIL Open Font License) er inkluderet i `static/vendor/`, så dashboardet virker uden internet.
