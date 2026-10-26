# tapo-uploader: dalla telecamera Tapo a YouTube

🌐 [English](README.md) · **Italiano**

Registra lo stream di una telecamera **TP-Link Tapo** (testato su C200) e carica da solo i
video su **YouTube come privati**, con una **playlist per ogni giorno**. Dopo l'upload il file
viene cancellato dal disco.

```
 Tapo C200 ──RTSP──▶ ffmpeg ──▶ segmenti .mp4 ──▶ YouTube API ──▶ playlist "Tapo 2026-10-26"
 (casa)                        (cartella recordings/)             (video privati, file cancellato)
```

Ci sono **due modalità**: la registrazione continua a segmenti (`tapo_uploader.py`, descritta
sotto) e quella **solo attorno ai movimenti con un video al giorno** (`tapo_motion.py`, vedi
[Modalità a movimento](#modalità-a-movimento-un-video-al-giorno)).

La telecamera non sa caricare nulla da sola: serve un computer sempre acceso (un VPS, un
Raspberry Pi, un mini PC) che legga lo stream e faccia l'upload. Questa guida racconta il
setup completo, passo per passo, com'è stato fatto davvero, compresi i problemi incontrati.

- [Cosa fa e cosa serve](#cosa-fa-e-cosa-serve)
- [1. La telecamera](#1-la-telecamera)
- [2. Google Cloud e YouTube](#2-google-cloud-e-youtube)
- [3. Installazione e prova sul tuo PC](#3-installazione-e-prova-sul-tuo-pc)
- [Dove farlo girare](#dove-farlo-girare)
- [4. Il server](#4-il-server)
- [5. Raggiungere la camera da internet](#5-raggiungere-la-camera-da-internet)
- [6. Verifica finale](#6-verifica-finale)
- [Modalità a movimento: un video al giorno](#modalità-a-movimento-un-video-al-giorno)
- [Modalità multi-account](#modalità-multi-account)
- [Dove vanno le registrazioni](#dove-vanno-le-registrazioni)
- [Uso quotidiano](#uso-quotidiano)
- [Configurazione](#configurazione)
- [Come funziona](#come-funziona)
- [Quota YouTube](#quota-youtube)
- [Sicurezza](#sicurezza)
- [Problemi comuni](#problemi-comuni)

---

## Cosa fa e cosa serve

`tapo_uploader.py run` avvia due thread:

1. **Registrazione.** ffmpeg legge lo stream RTSP con `-c copy` (il video non viene
   ricodificato, quindi quasi nessun carico CPU) e scrive file `AAAAMMGG_HHMMSS.mp4` in
   `recordings/`. I tagli sono allineati all'orologio: con segmenti da 6 ore si chiudono a
   00:00, 06:00, 12:00 e 18:00. Se lo stream cade, ffmpeg riparte da solo.
2. **Upload.** Ogni minuto cerca i segmenti completi, li carica come video `private` col
   titolo `Tapo AAAA-MM-GG HH:MM-HH:MM`, li aggiunge alla playlist `Tapo AAAA-MM-GG` (creata
   se manca) e **cancella il file**. Se l'upload fallisce, il file resta e viene ritentato.

| Cosa serve | Note |
|---|---|
| Telecamera Tapo con RTSP | C200 e simili. Serve l'"Account camera" (vedi passo 1). |
| Un computer sempre acceso | Linux consigliato (VPS, Raspberry Pi). Python 3.10 o più recente e ffmpeg. |
| Un account Google con un canale YouTube | I video finiscono nel suo canale. |
| Accesso al router (solo se il server non è in casa) | Per aprire una porta verso la camera. |

Spazio disco: a 640×360 un segmento da 6 ore pesa circa 0,4 GB. Servono pochi GB liberi,
perché i file vengono cancellati dopo l'upload.

---

## 1. La telecamera

1. Nell'app Tapo apri la camera, poi **Impostazioni (ingranaggio) → Avanzate → Account camera**.
   Crea un nome utente e una password **solo per la camera**. Non sono quelli del tuo account
   Tapo/cloud, ed è un errore comune: con quelli ricevi `401 Unauthorized`.
2. Cerca l'indirizzo IP della camera nell'app (Info dispositivo) o nel router, e meglio
   ancora assegnale un IP fisso (prenotazione DHCP) nel router.
3. Imposta la qualità video su **Alta** (Impostazioni → Qualità video). Di default può essere
   bassa: nel nostro caso entrambi gli stream erano 640×360.
4. L'indirizzo dello stream è:

   ```
   rtsp://UTENTE:PASSWORD@IP_CAMERA:554/stream1     (qualità alta)
   rtsp://UTENTE:PASSWORD@IP_CAMERA:554/stream2     (qualità bassa)
   ```

   Se la password ha caratteri speciali (`!`, `@`, `:`) vanno codificati nell'URL. Lo script
   lo fa da solo, a mano è più semplice usare una password solo con lettere e numeri.

> **Limite importante:** la camera accetta **una sola sessione RTSP per stream**. Se il servizio
> sta registrando `stream1`, un secondo programma che apre `stream1` riceve
> `Operation not permitted`. Per fare prove mentre il servizio gira usa `stream2`
> (`CAMERA_STREAM=stream2`) oppure ferma prima il servizio.

---

## 2. Google Cloud e YouTube

Lo script carica sul tuo canale con l'API ufficiale (YouTube Data API v3), quindi serve un
piccolo "progetto" Google che identifichi l'app. È gratis.

### 2.1 Progetto e API

1. Vai su <https://console.cloud.google.com/projectcreate>, nome `tapo-uploader`, **Crea**.
2. Apri <https://console.cloud.google.com/apis/library/youtube.googleapis.com> e premi
   **Abilita** (con il progetto appena creato selezionato in alto).

### 2.2 Schermata di consenso OAuth

1. Apri <https://console.cloud.google.com/auth/overview> e premi **Inizia**.
2. Nome app `tapo-uploader`, email di assistenza = la tua.
3. Pubblico: **Esterno**. Email di contatto = la tua. Accetta e **Crea**.
4. In **Pubblico → Utenti di test → Add users** aggiungi **il tuo account Google**, quello
   proprietario del canale YouTube.

### 2.3 Credenziali

1. **Client → Crea client**. Tipo: **App desktop**, nome `tapo-uploader`. **Crea**.
2. Premi **Scarica JSON** e salva il file come `client_secret.json` nella cartella del progetto.
   È un segreto: non committarlo (il `.gitignore` lo esclude).

### 2.4 Pubblicare l'app (importante)

Finché l'app resta in **Test**, il token di accesso **scade dopo 7 giorni** e lo script smette
di caricare finché non rifai il login. Per evitarlo bisogna pubblicare l'app ("In produzione").
Per uso personale non serve alcuna verifica da parte di Google, ma il pulsante
**Pubblica app** resta disattivato finché in **Branding** non ci sono:

- un link alla **home page** dell'app,
- un link alla **privacy policy**,
- il **dominio autorizzato** corrispondente.

Non serve un sito vero: bastano due pagine statiche. Il modo più veloce è **GitHub Pages**:

1. Crea un repository pubblico (anche questo va bene) con due file, `index.html` (una riga
   su cosa fa l'app) e `privacy.html` (cosa accede, cosa viene salvato, come revocare
   l'accesso). In questo repository ci sono entrambi, da copiare e adattare.
2. In *Settings → Pages* scegli il ramo `main`, cartella `/`.
3. Dopo un minuto le pagine sono su `https://TUOUTENTE.github.io/NOME-REPO/`.
4. In Google Cloud, **Branding → Dominio app**: inserisci home page e privacy policy; in
   **Domini autorizzati → Aggiungi dominio** metti `TUOUTENTE.github.io`. Salva.
5. **Pubblico → Pubblica app → Conferma** ("Eseguire il push in produzione?").

> Fai la pubblicazione **prima** di creare il token (passo 3). Un token creato mentre l'app
> era in Test può restare soggetto alla scadenza di 7 giorni anche dopo la pubblicazione: per
> sicurezza cancella `token.json` e rifai `auth` dopo aver pubblicato.

---

## 3. Installazione e prova sul tuo PC

Questi passi valgono su qualunque sistema (Windows, macOS, Linux). Servono Python 3.10+ e
**ffmpeg** nel PATH (`ffmpeg -version` per controllare).

```bash
git clone https://github.com/LuigiElleBalotta/tapo-uploader.git
cd tapo-uploader
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # poi compila CAMERA_USER, CAMERA_PASS, CAMERA_HOST
```

Metti `client_secret.json` (passo 2.3) nella cartella, poi:

```bash
python tapo_uploader.py check   # 1. la camera risponde? mostra codec e risoluzione
python tapo_uploader.py auth    # 2. login Google, una volta sola: crea token.json
python tapo_uploader.py test    # 3. registra 60 s e li carica: verifica tutto
```

- **`check`** deve stampare `codec_name=h264`, la risoluzione e l'audio. Se dà
  `401 Unauthorized` le credenziali sono sbagliate; `Operation not permitted` vuol dire che
  lo stream è già occupato (vedi sopra).
- **`auth`** stampa un indirizzo: aprilo in un browser **sullo stesso computer**, scegli
  l'account Google del canale e consenti l'accesso a YouTube. Il browser mostra
  "The authentication flow has completed" e compare `token.json`. Se in Test vedi
  "Google non ha verificato questa app": *Avanzate → Vai a tapo-uploader*.
- **`test`** registra 60 secondi, li carica come video privato e crea la playlist del
  giorno. Controlla su YouTube che il video ci sia e poi cancellalo dallo Studio.

Se il server non ha un browser, fai `auth` sul tuo PC e copia `token.json` sul server.

---

## Dove farlo girare

Il programma deve restare acceso 24 ore su 24 e deve poter raggiungere la camera. Le opzioni,
dalla più semplice:

| Dove | Costo indicativo | Come raggiunge la camera | Note |
|---|---|---|---|
| **Raspberry Pi / mini PC in casa** | una tantum, 15–50 € | in LAN | **Consigliato**: niente port forwarding, niente DDNS, niente IP da proteggere. |
| **VPS** (come in questa guida) | pochi euro al mese | port forwarding limitato all'IP fisso del VPS | Passi 4 e 5. È la configurazione collaudata. |
| **Render** (Background Worker) | da circa 7 $/mese, più 0,25 $/GB di disco | port forwarding verso gli IP condivisi di Render | Immagine Docker inclusa, **non collaudata**. Vedi sotto. |
| **Render gratuito** (web service) | 0 € | port forwarding verso gli IP condivisi di Render | Collaudato fino all'avvio, non con la camera. Disco effimero: si possono perdere clip. Vedi sotto. |
| **Vercel** | – | – | **Non adatto.** Vedi sotto. |

Le altre piattaforme con container (Fly.io, Railway) seguono lo stesso schema di Render: il
`Dockerfile` funziona ovunque, cambiano prezzi e rete.

### Render

Render può far girare un processo sempre acceso (un *Background Worker*) da un'immagine
Docker. Il repository contiene `Dockerfile` e `render.yaml`. Cose da sapere:

- **Costo.** Il worker più piccolo (`0.5c-512mb`: 0,5 CPU e 512 MB) è elencato a 7 $/mese. Il
  piano gratuito di Render è solo per i servizi web, non per i worker. Con ffmpeg in `-c copy` la
  CPU non è il limite; la RAM non l'ho misurata su Render.
- **Disco.** Il disco del container si perde a ogni riavvio e a ogni deploy. Senza un **disco
  persistente** (0,25 $/GB al mese, 1 GB bastano; si attacca a un worker) si perdono il token, lo
  stato e i clip della giornata prima delle 21:00. Il `render.yaml` ne chiede uno montato su
  `/data`, e lo script lo usa grazie a `DATA_DIR`.
- **Segreti.** Su un container non ci sono file da copiare: `token.json` e `client_secret.json` si
  passano come variabili d'ambiente `YOUTUBE_TOKEN_JSON` e `YOUTUBE_CLIENT_SECRET_JSON` (il contenuto
  dei file). Alla prima partenza lo script li scrive su `/data` con permessi `600`; dopo, il token
  rinnovato resta sul disco. Il login (`auth`) si fa una volta sul tuo PC.
- **La rete è il punto debole.** Gli IP in uscita di Render sono **condivisi da tutti i servizi
  della stessa regione** e un servizio può usare uno qualsiasi degli indirizzi del suo intervallo
  (vedi [la documentazione di Render](https://render.com/docs/outbound-ip-addresses)). Per
  restringere l'accesso alla camera dovresti aprire il port forwarding a **tutti gli intervalli
  della regione**, quindi anche ad altri clienti di Render, e il router ZTE accetta un solo
  intervallo per regola. Gli IP dedicati esistono ma, dalle informazioni che ho trovato, sono
  a pagamento sui piani Pro/Scale (circa 100 $/mese per un gruppo di IP): per questo uso non
  conviene. Se scegli Render, la porta esposta dev'essere protetta da una password **lunga e
  unica**, con porta esterna non standard; oppure collega casa e Render con una VPN, cosa che non
  ho verificato.

Passi: collega il repository a Render (**New → Blueprint**), compila le variabili che chiede
(`CAMERA_USER`, `CAMERA_PASS`, `CAMERA_HOST`/`CAMERA_PORT`, `ONVIF_HOST`/`ONVIF_PORT`,
`YOUTUBE_TOKEN_JSON`, `YOUTUBE_CLIENT_SECRET_JSON`), apri sul router le porte verso gli IP di
Render e guarda i log del worker. Render chiede le variabili solo alla creazione del Blueprint:
cambiarle dopo si fa dal pannello. Per la registrazione continua invece di quella a movimento
decommenta `dockerCommand` nel `render.yaml`.

### Render gratuito (senza VPS)

Per chi non ha un server. Il **VPS resta la scelta principale**: questa è un'alternativa a costo
zero, con limiti precisi. Il deploy come *web service* sul piano `free` è stato provato su Render
fino all'avvio (build, `health endpoint on port 10000`, `ffmpeg start`); **la registrazione dalla
camera tramite Render non è stata verificata**, perché richiede le regole del router descritte
sotto.

Come funziona: il programma apre una piccola porta HTTP (`/health`, attiva quando Render imposta
`PORT`) e un servizio di monitoraggio gratuito (UptimeRobot, cron-job.org) la chiama ogni 5
minuti. Per la [documentazione di Render](https://render.com/docs/free) un servizio gratuito
dorme dopo 15 minuti **senza traffico in entrata**; il traffico in uscita non conta, quindi serve
una chiamata dall'esterno. Le 750 ore gratuite al mese bastano per un solo servizio sempre acceso.

**Passi (quelli fatti davvero, dal pannello):**

1. Su [dashboard.render.com](https://dashboard.render.com) fai **New → Web Service**, scheda
   **Public Git Repository**, incolla l'URL del repository e premi **Connect**. Render riconosce il
   `Dockerfile` da solo.
2. In **Compute** scegli la riga **$0 / month (0,1 CPU, 512 MB)**. Il piano preselezionato è quello
   da 7 $/mese: se lo lasci, paghi.
3. In **Environment Variables** premi **Add from .env** e incolla le righe qui sotto con i tuoi
   valori. Il token e il client secret vanno su **una sola riga**, come JSON compatto
   (`python -c "import json;print(json.dumps(json.load(open('token.json')),separators=(',',':')))"`).
   ```
   CAMERA_USER=...
   CAMERA_PASS=...
   CAMERA_HOST=il-tuo-nome-ddns.example.com
   CAMERA_PORT=porta esterna RTSP
   ONVIF_HOST=il-tuo-nome-ddns.example.com
   ONVIF_PORT=porta esterna ONVIF
   CAMERA_STREAM=stream1
   DATA_DIR=/data
   TZ=Europe/Rome
   YOUTUBE_TOKEN_JSON=...
   YOUTUBE_CLIENT_SECRET_JSON=...
   ```
4. **Deploy web service**. La build dura circa un minuto e mezzo. Nei log devi vedere
   `health endpoint on port 10000` e `ffmpeg start`. L'avviso `file_cache is only supported with
   oauth2client<4.0.0` è innocuo.
5. Dalla pagina del servizio prendi l'URL (`https://NOME.onrender.com`) e, con il pulsante
   **Connect → Outbound**, gli intervalli IP da dare al router (nel mio caso `74.220.51.0/24` e
   `74.220.59.0/24`: i tuoi dipendono dalla regione e possono essere diversi).
6. Crea sul router le regole per le due porte della camera, una per intervallo (vedi sotto).
7. Crea il monitor (UptimeRobot, cron-job.org) su `https://NOME.onrender.com/health` ogni 5 minuti.

**Se già usi un VPS con questa camera:** la camera accetta una sola sessione RTSP per stream. Se
VPS e Render registrano entrambi su `stream1`, il secondo che si collega riceve
`Operation not permitted`. Fai girare un solo servizio per stream, oppure imposta
`CAMERA_STREAM=stream2` su Render.

Il Blueprint `render.free.yaml` non l'ho provato: aprendo il Blueprint con il `render.yaml` di
default (worker da 7 $/mese) Render chiede una carta di pagamento. Il web service creato a mano,
come sopra, non l'ha chiesta.

**Limiti e rischi, da leggere prima:**

| Limite | Conseguenza |
|---|---|
| Il disco si azzera a ogni riavvio, deploy o spin-down | I clip della giornata non ancora caricati si **perdono**. Render può riavviare il servizio in qualsiasi momento. |
| Se il servizio dorme (monitor fermo, ore finite) | Nessuna registrazione finché non si risveglia (circa un minuto dopo la prima richiesta). |
| Ore gratuite esaurite | Tutti i servizi gratuiti si sospendono fino al mese dopo. Con un solo servizio e il monitor non succede, con due sì. |
| Render può sospendere i servizi gratuiti con traffico in uscita molto alto | Qui il traffico è di pochi MB al giorno, ma le regole possono cambiare. |
| Il token YouTube rinnovato non viene salvato | Il token arriva da `YOUTUBE_TOKEN_JSON`; a ogni riavvio si riparte da quello. Funziona finché non viene revocato. |
| RAM e CPU non documentate per il piano gratuito | Non misurate: ffmpeg in `-c copy` è leggero, ma non è verificato. |
| Il monitor esterno è un'altra dipendenza | Se il servizio di monitoraggio cambia le sue regole, il servizio dorme. |
| **IP di Render condivisi e non fissi** | Vedi sotto: è il rischio più serio. |

**Il rischio più serio è la sicurezza della camera.** Gli IP in uscita di Render sono condivisi da
tutti i servizi della regione e non sono nella documentazione: li trovi nel pannello del servizio,
**Connect → Outbound**. Hai due strade, entrambe con un prezzo:

1. **Regole nel router per ogni intervallo** (consigliata). Una regola di port forwarding per
   intervallo e per porta, con origine limitata a quell'intervallo. Anche così resti esposto a
   chiunque usi lo stesso intervallo su Render, e gli intervalli possono cambiare: ricontrolla di
   tanto in tanto. Non ho verificato che il tuo router accetti più regole sulla stessa porta
   esterna con origini diverse; se non lo fa, usa porte esterne diverse per ogni regola.
2. **Porte aperte a tutti.** Solo la password protegge la camera: usa una password **lunga e
   unica** (mai quella di altri account), una porta esterna non standard, e tieni d'occhio i
   tentativi di accesso. Sconsigliato.

Se la connessione alla camera cade, lo script riprova da solo con attesa crescente: si perde solo
quel tratto. Se invece cambia il tuo IP di casa, il nome DDNS (passo 5.1) segue l'aggiornamento.

Per avere gli stessi risultati del VPS senza questi limiti servono un disco persistente (a
pagamento, vedi sopra) o un Raspberry in casa.

### Vercel

Qui la risposta è no, ed è una questione di come funziona la piattaforma, non di configurazione.

- **Le Functions hanno un tempo massimo.** Con Fluid compute il limite è 300 s su Hobby e 800 s su
  Pro, con 1800 s in beta ([limiti](https://vercel.com/docs/functions/limitations)). Una
  funzione risponde a una richiesta e viene spenta: non può tenere ffmpeg collegato alla camera
  tutto il giorno.
- **Perché i progetti NestJS "non serverless" funzionavano.** Un'app NestJS su Vercel viene
  eseguita comunque come funzione: serve richieste HTTP, che durano pochi secondi. Un servizio che
  registra in continuo è un'altra cosa.
- **Il disco è temporaneo.** Non c'è un posto dove tenere l'anello di video né i clip fino alle 21.
- **Vercel Sandbox** è una microVM che può far girare processi in background, ma dura al massimo
  45 minuti su Hobby o qualche ora su Pro, e i file spariscono quando si ferma. Si potrebbe
  concatenare una sandbox dopo l'altra e salvare i clip su Vercel Blob, ma resterebbero lacune ad
  ogni passaggio, i costi di un processo sempre acceso e **IP in uscita non fissi**, quindi il
  port forwarding non si potrebbe restringere. Non lo consiglio.
- **Vercel Workflows** mette in pausa e riprende elaborazioni lunghe, ma non è un posto dove far
  girare un processo di registrazione.

Vercel può invece servire per una pagina di stato o per un'API che legga lo stato dei video, con
la registrazione che gira altrove.

---

## 4. Il server

Esempio su Ubuntu/Debian (un VPS, un Raspberry Pi, un mini PC). Sostituisci `UTENTE` con il
tuo utente Linux.

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg python3-venv      # python3-venv serve davvero su Ubuntu

mkdir -p ~/tapo-uploader && cd ~/tapo-uploader
# copia qui: tapo_uploader.py, tapo_motion.py, camera_context.py, camera_worker.py, requirements.txt, .env, client_secret.json, token.json
chmod 600 .env client_secret.json token.json     # solo tu puoi leggerli

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Il `.env` del server è quello del PC, con `CAMERA_HOST` e `CAMERA_PORT` che puntano alla
camera **vista da internet** (passo 5), non all'IP di casa.

### Servizio systemd

Così parte all'avvio e si riavvia da solo se si ferma (non serve un cron).

```bash
sudo cp deploy/tapo-uploader.service /etc/systemd/system/
sudo nano /etc/systemd/system/tapo-uploader.service   # imposta UTENTE e i percorsi
sudo systemctl daemon-reload
sudo systemctl enable tapo-uploader
```

**Non avviarlo ancora**: prima il passo 5 e la verifica del passo 6.

Il file imposta `Environment=TZ=Europe/Rome`: senza, un server in UTC taglia i segmenti
all'una di notte ora italiana e cambia playlist a mezzanotte UTC. Cambia il fuso se serve.

---

## 5. Raggiungere la camera da internet

Se il server è nella stessa rete della camera, salta questo passo e usa l'IP locale.
Altrimenti la camera, che sta dietro al router di casa, va resa raggiungibile **solo dal
tuo server**.

> L'app Tapo funziona da fuori casa perché la camera si collega *in uscita* ai server
> TP-Link. Questo non vale per RTSP: serve una porta aperta sul router.

### 5.1 Nome fisso per l'IP di casa (DDNS)

L'IP pubblico di casa di solito cambia. Un servizio DDNS gratuito dà un nome fisso che segue
l'IP. Con **FreeDNS** (<https://freedns.afraid.org>, gratuito, senza conferme mensili):

1. Registrati, poi **Subdomains → Add**: tipo `A`, scegli un dominio libero e un nome
   difficile da indovinare (es. `cam-` più caratteri casuali). Destinazione: il tuo IP attuale.
2. In **Dynamic DNS** copia il token del tuo record: è la parte dopo il `?` del "Direct URL"
   (la chiamano "hash").
3. Nel router, sezione **DDNS**: provider `freedns`, nome dominio = il sottodominio
   completo, hash = il token. Applica: lo stato deve dire che l'aggiornamento è andato bene.

Controlla che il nome risolva nel tuo IP: `nslookup il-tuo-nome.dominio.com`.

> Se l'IP "WAN" mostrato dal router è diverso dall'IP pubblico che vedi su un sito tipo
> `ifconfig.me`, sei dietro **CGNAT** e nessun port forwarding potrà funzionare: usa una VPN
> (Tailscale, WireGuard) tra casa e server, oppure fai girare lo script in casa.

### 5.2 Port forwarding limitato al server

Nel router: **Port Forwarding → nuova regola**.

| Campo | Valore |
|---|---|
| Protocollo | TCP |
| Porta WAN (esterna) | una porta alta a scelta, es. `38554`, non la 554 standard |
| Host LAN | IP della camera |
| Porta host LAN | `554` |
| **IP host remoti** | **l'IP pubblico del tuo server** (stesso valore come inizio e fine intervallo) |

Il campo "IP host remoti" è quello che conta: così la camera non è raggiungibile da nessun
altro indirizzo di internet. Se il router non lo permette, valuta la VPN.

Poi nel `.env` del server:

```
CAMERA_HOST=il-tuo-nome.dominio.com
CAMERA_PORT=38554
```

### 5.3 Prova dal server

```bash
bash -c '</dev/tcp/il-tuo-nome.dominio.com/38554' && echo PORTA_APERTA
.venv/bin/python tapo_uploader.py check
```

Dall'esterno (per esempio col telefono in 5G) la stessa porta deve risultare **chiusa**.

---

## 6. Verifica finale

```bash
cd ~/tapo-uploader
.venv/bin/python tapo_uploader.py test      # registra 60 s e carica, dal server
sudo systemctl start tapo-uploader
systemctl status tapo-uploader              # deve essere "active (running)"
journalctl -u tapo-uploader -f              # log in tempo reale
ls -la recordings                           # il segmento in corso
```

Ricorda il limite di una sessione per stream: lancia `check` e `test` **prima** di avviare il
servizio, oppure con `CAMERA_STREAM=stream2`.

Il primo upload automatico avviene dopo la chiusura del primo segmento (alla prossima ora
00, 06, 12 o 18) e il file sparisce da `recordings/`. Su YouTube trovi il video privato e la
playlist del giorno. **Le playlist private non compaiono nella pagina pubblica del tuo
canale**: cercale in **YouTube → Tu → Playlist** oppure in
Studio → Contenuti → Playlist.

---

## Modalità a movimento: un video al giorno

Alternativa alla registrazione continua (`tapo_uploader.py`): `tapo_motion.py` registra **solo
quando la camera rileva un movimento** e carica **un unico video al giorno**, per esempio alle 21.
Con la registrazione continua si caricano 4 video al giorno e si tiene tutto; qui si tiene solo
ciò che è successo.

```
 ffmpeg ──▶ anello di segmenti da 10 s (solo l'ultimo minuto) ──┐
 camera ──ONVIF (eventi)──▶ movimento? ──▶ clip: 30 s prima + 5 min dopo ──▶ events/AAAAMMGG/
                                                                        │
                       alle 21:00: tutti i clip del giorno ──▶ un video privato ──▶ file cancellati
```

**Regole:**
- Il primo movimento apre un evento: il clip va da `MOTION_PRE_SECONDS` (30 s) **prima** a
  `MOTION_POST_SECONDS` (5 min) **dopo**.
- Un movimento che arriva negli **ultimi `MOTION_EXTEND_WITHIN` secondi (30)** della finestra la
  prolunga di altri 5 minuti da quel momento (per esempio un movimento al 4:50). Lo stesso se il
  movimento è ancora in corso. I movimenti nelle fasi precedenti della finestra vengono ignorati.
- Alle `UPLOAD_AT` (21:00) i clip del giorno vengono uniti **senza ricodificare** in un solo video
  privato. Il titolo è `Tapo 2026-10-26 eventi 07:12-20:55 (5)` e la **descrizione elenca l'ora di
  ogni evento** (`0:00 Movimento alle 07:12:03`...): YouTube la mostra come capitoli. Poi i file
  vengono cancellati. Se un giorno non c'è stato nessun evento non si carica nulla. Un evento
  chiuso dopo le 21:00 finisce nel video successivo.
- Senza movimenti il disco non si riempie: l'anello tiene circa un minuto di video.

**Cosa serve in più:**

1. **Rilevamento attivo nell'app Tapo** (Impostazioni → Rilevamento e allarmi). Gli eventi
   dipendono da cosa è acceso: `IsMotion` = rilevamento del movimento, `IsPeople` = rilevamento
   persone. Si scelgono con `MOTION_EVENTS` (default: tutti e due).
2. **ONVIF raggiungibile dal server.** Gli eventi passano dalla porta 2020 della camera. In LAN non
   serve nulla; da remoto serve **una seconda regola di port forwarding** come quella del passo 5.2:
   porta esterna `38020` (a scelta) verso `IP_CAMERA:2020`, TCP, **solo dall'IP del server**. Nel
   `.env` del server:

   ```
   ONVIF_HOST=il-tuo-nome.dominio.com
   ONVIF_PORT=38020
   ```

   Su alcuni router (ZTE H388X) la regola creata diventa attiva solo dopo aver premuto **Applica una
   seconda volta**: verifica dal server con `bash -c '</dev/tcp/il-tuo-nome.dominio.com/38020'`.
3. **Servizio al posto di quello continuo.** La camera accetta una sola sessione RTSP per stream:
   fermane uno prima di avviare l'altro.

   ```bash
   sudo systemctl disable --now tapo-uploader
   sudo cp deploy/tapo-motion.service /etc/systemd/system/    # imposta UTENTE e i percorsi
   sudo systemctl daemon-reload && sudo systemctl enable --now tapo-motion
   journalctl -u tapo-motion -f
   ```

**Comandi:**

```bash
python tapo_motion.py listen 60    # stampa gli eventi di movimento che la camera invia (prova ONVIF)
python tapo_motion.py run          # registra attorno ai movimenti, carica una volta al giorno
python tapo_motion.py upload       # unisce e carica subito i clip in attesa
```

**Prova rapida con tempi ridotti**, da un PC in LAN (usa `stream2` se il servizio occupa `stream1`):

```bash
CAMERA_STREAM=stream2 MOTION_POST_SECONDS=20 MOTION_EXTEND_WITHIN=10 UPLOAD_AT=00:00 \
  python tapo_motion.py run
```

Cammina davanti alla camera: nel log compaiono `motion: recording...`, poi `clip saved` e,
entro un minuto, l'upload del video del giorno (`UPLOAD_AT=00:00` lo rende subito dovuto).

**Cose da sapere:**
- I 30 secondi di anticipo hanno una tolleranza di circa 10 secondi (segmenti da 10 s, tagli sui
  keyframe).
- Se il servizio si riavvia durante un evento, quel clip va perso.
- Con la camera ferma `listen` non stampa nulla: la camera segnala solo i cambi di stato.

---

## Modalità multi-account

Una modalità **opzionale** per un gruppo di persone fidate (amici, famiglia) su un solo server. Ognuno accede,
collega **la propria camera** e **il proprio canale YouTube**, e vede solo i propri video e lo stato della sua
camera in una pagina personale. Per chi la usa da solo non cambia niente: senza `MULTIUSER=1` i programmi qui
sopra funzionano come prima, con lo stesso `.env`.

Cosa aggiunge: una piccola app web (`python -m tapo_web run`) con un database SQLite, un supervisore che avvia un
registratore per ogni camera, una guida del router per ogni amico (passi manuali e un testo da incollare in un
assistente AI), il pulsante "Prova connessione" e una pagina per l'amministratore. L'accesso è configurabile:
account locali e/o **Nextcloud** (i suoi gruppi diventano ruoli). Password e token sono salvati cifrati.

> **Quota, da leggere prima.** Tutti gli utenti condividono la quota YouTube del progetto Google (10.000 unità al
> giorno di default). Un upload giornaliero completo costa circa 1.700 unità: **un progetto basta per 5 o 6
> utenti** con un upload al giorno. Per superare il limite: chiedere a Google più quota, far caricare qualcuno a
> giorni alterni, oppure attivare `ALLOW_OWN_GOOGLE_CLIENT` così un utente porta il proprio progetto Google. La
> pagina amministratore mostra la quota usata oggi.

### Installazione

1. **Installa** sul server: copia il progetto (compresa la cartella `tapo_web`), poi
   `python3 -m venv .venv && .venv/bin/pip install -r requirements-web.txt`.
2. **Progetto Google.** In Google Cloud crea un client OAuth di tipo **Applicazione web** (non Desktop) con l'URI di
   reindirizzamento autorizzato `https://TUO-INDIRIZZO/youtube/callback`. Metti il suo JSON in
   `YOUTUBE_CLIENT_SECRET_JSON`. L'app deve essere *In produzione* (passo 2.4); oltre 100 utenti Google richiede la
   verifica. Ora altre persone usano la tua app: aggiorna la schermata di consenso e la pagina della privacy policy.
3. **Chiavi e impostazioni.** Copia il blocco multi-account di `.env.example` e compilalo. Genera le due chiavi:
   `python -c "from tapo_web.crypto import Vault; print(Vault.generate_key())"` per `ENCRYPTION_KEY`, e una stringa
   casuale lunga per `SECRET_KEY`. Conserva una copia di `ENCRYPTION_KEY`: senza, le password e i token salvati non
   si possono più leggere. Imposta `PUBLIC_URL` (HTTPS) e `SERVER_PUBLIC_IP` (l'indirizzo che gli amici
   autorizzeranno nel router).
4. **Primo amministratore.** `python -m tapo_web create-admin`, oppure imposta `ADMIN_USERNAME` e `ADMIN_PASSWORD`
   per il primo avvio.
5. **Nextcloud (opzionale).** In Nextcloud, Impostazioni, Sicurezza, *Client OAuth 2.0*: aggiungi un client con
   l'URI `https://TUO-INDIRIZZO/auth/nextcloud/callback`, poi imposta `NEXTCLOUD_URL`, `NEXTCLOUD_CLIENT_ID` e
   `NEXTCLOUD_CLIENT_SECRET`. I ruoli vengono dai gruppi: i membri di `NEXTCLOUD_ADMIN_GROUP` (default `admin`) sono
   amministratori, i membri di `NEXTCLOUD_USER_GROUP` possono usare il servizio. Se quel gruppo non è impostato,
   possono entrare solo gli amministratori. I ruoli si ricontrollano a ogni accesso: chi esce dal gruppo viene
   escluso al login successivo e resta fuori finché un amministratore non lo riattiva.
6. **Avvio** dietro un reverse proxy HTTPS (nginx, Caddy, aaPanel): `python -m tapo_web run` (porta `PORT`, default
   8080), oppure come servizio con `deploy/tapo-web.service`, oppure in Docker con
   `CMD ["python", "-u", "-m", "tapo_web", "run"]`. Dietro un reverse proxy imposta `TRUSTED_PROXY_HOPS=1`, altrimenti ogni visitatore sembra il proxy e i limiti ai tentativi di accesso non li distinguono. Non è pensata per il piano gratuito di Render (disco effimero).

### Aggiungere la camera di un amico

L'amico accede, poi **Connect YouTube** (il suo canale), poi **Add a camera** (il suo nome DDNS e l'account camera
dell'app Tapo). Il server assegna due porte esterne casuali e mostra una guida del router: apri quelle due porte
**solo per `SERVER_PUBLIC_IP`**. **Test connection** verifica la porta, il flusso video e gli eventi di movimento;
solo allora parte la registrazione.

### Da sapere

- Gli indirizzi delle camere devono essere pubblici: indirizzi privati, di loopback e link-local sono rifiutati, così
  nessuno può usare il server per raggiungere la sua rete. Imposta `ALLOW_PRIVATE_HOSTS=1` solo in una rete domestica.
- Una camera per utente di default (`MAX_CAMERAS_PER_USER`), 20 in totale (`MAX_CAMERAS_TOTAL`).
- Cancellare una camera cancella anche le sue registrazioni sul server.
- Gli upload delle varie camere sono distribuiti nel tempo (`UPLOAD_AT`, poi `UPLOAD_STAGGER_MINUTES` di distanza).
- I dati e i token di un utente disattivato vengono cancellati dopo `RETENTION_DAYS` (30). Se il collegamento
  YouTube di qualcuno manca o è revocato, i suoi clip restano 7 giorni e poi vengono scartati.
- Tutte le impostazioni sono elencate in `.env.example`.

---

## Dove vanno le registrazioni

Di default il video del giorno va su YouTube. Puoi mandarlo altrove:

| Destinazione | `STORAGE` | Cosa serve |
|---|---|---|
| YouTube (default) | `youtube` | la configurazione Google descritta sopra |
| WebDAV: Nextcloud, ownCloud, Synology... | `webdav` | `WEBDAV_URL`, `WEBDAV_USER`, `WEBDAV_PASS` (per Nextcloud con autenticazione a due fattori, una password per app), facoltativo `WEBDAV_FOLDER` |
| Una cartella sulla macchina che esegue il programma | `local` | `LOCAL_ROOT` |
| Tutto ciò che supporta rclone: Mega, storage compatibili S3 (Backblaze B2, Wasabi, MinIO), Dropbox, OneDrive... | `rclone` | il programma `rclone`, un remote creato con `rclone config`, `RCLONE_REMOTE` (facoltativi `RCLONE_CONFIG`, `RCLONE_FOLDER`) |

Imposta le variabili nel `.env` (sono tutte elencate in `.env.example`) e riavvia. I video sono salvati come
`<cartella>/Tapo AAAA-MM-GG/<titolo>.mp4`, con i capitoli in un file `.chapters.txt` accanto a ognuno. Il file viene
scritto con un nome temporaneo e rinominato a upload finito, quindi un upload interrotto non sembra mai completo, e
i clip restano sulla macchina finché l'upload non riesce. Vale per la modalità a movimento (`tapo_motion.py`); la
modalità continua (`tapo_uploader.py`) usa sempre YouTube.

**Mega** funziona tramite il backend `mega` di rclone. L'autore **non l'ha provato**: Mega non offre un'API ufficiale
per questo uso, quindi leggi la documentazione di rclone per i suoi limiti prima di farci affidamento.

**In modalità multi-account** il server sceglie il default (`STORAGE` e le variabili `STORAGE_*`) e ogni utente lo
ottiene senza fare nulla, anche senza accedere con Nextcloud. Le destinazioni che stanno sul server (WebDAV, cartella,
rclone) hanno una sotto-cartella per ogni camera, così nessuno vede i video di un altro. Con `ALLOW_OWN_STORAGE=1`
ogni utente può scegliere, nella pagina **Recordings**, YouTube o il proprio WebDAV (`STORAGE_ALLOWED` restringe la
scelta; l'indirizzo deve essere pubblico e `https`). `local` e `rclone` sono solo per il server, di proposito: un
percorso o una configurazione rclone scelti da un utente gli darebbero accesso al server stesso. Se una scelta salvata
non è più permessa, si usa il default del server e la pagina lo dice. Solo YouTube consuma la quota condivisa. Con una
destinazione sul server **il server conserva i video**: pianifica lo spazio disco e chi può leggerli.

**Guardare:** in modalità multi-account la pagina personale mostra ogni video come una card con due azioni.
**Watch here** (Guarda qui) apre un player nella pagina (il player incorporato di YouTube, oppure l'app stessa che invia il
file, con la possibilità di scorrere, solo al suo proprietario). **Open the video** (Vai al video) porta al video su
YouTube o al file nel tuo cloud (niente per cartella locale o rclone). L'audio viene convertito in AAC durante la
registrazione e il video è copiato così com'è: tieni la camera su H.264, perché i file H.265 potrebbero non partire in
alcuni browser.

---

## Uso quotidiano

```bash
sudo systemctl status tapo-uploader       # stato
journalctl -u tapo-uploader -f            # log
sudo systemctl restart tapo-uploader      # dopo aver cambiato .env o lo script
sudo systemctl stop tapo-uploader         # fermarlo
ls -la ~/tapo-uploader/recordings         # segmenti in attesa o in registrazione
```

- Un riavvio chiude il segmento corrente e ne apre uno nuovo, quindi viene caricato anche un
  video più breve.
- I segmenti sono MP4 "frammentati": restano riproducibili anche se ffmpeg viene
  interrotto di colpo (riavvio, spegnimento). Al massimo si perdono gli ultimi secondi.

---

## Configurazione

File `.env` (da `.env.example`, **mai** nel repository):

| Variabile | Significato |
|---|---|
| `CAMERA_USER`, `CAMERA_PASS` | Account camera creato nell'app Tapo (non l'account cloud). |
| `CAMERA_HOST`, `CAMERA_PORT` | In LAN l'IP della camera e `554`. Da remoto nome DDNS e porta esterna. |
| `CAMERA_STREAM` | `stream1` (alta qualità) o `stream2` (bassa). |
| `SEGMENT_SECONDS` | Durata di ogni file, default `21600` (6 ore). |
| `RECORD_DIR` | Cartella dei file in attesa di upload, default `recordings`. |
| `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_TOKEN` | Nomi dei file `client_secret.json` e `token.json`. |
| `YOUTUBE_PRIVACY` | `private` (default), `unlisted` o `public`. |
| `PLAYLIST_PREFIX` | Prefisso di titoli e playlist (default `Tapo`). |
| `DATA_DIR` | Cartella di token, stato e video in attesa (default: la cartella del progetto). In un container è il disco persistente, es. `/data`. |
| `YOUTUBE_TOKEN_JSON`, `YOUTUBE_CLIENT_SECRET_JSON` | Contenuto di `token.json` e `client_secret.json`, per i container: se i file mancano vengono creati da qui. |

Solo per `tapo_motion.py` (valori di default tra parentesi):

| Variabile | Significato |
|---|---|
| `MOTION_PRE_SECONDS` (30) | Secondi registrati prima del primo movimento. |
| `MOTION_POST_SECONDS` (300) | Secondi registrati dopo il movimento. |
| `MOTION_EXTEND_WITHIN` (30) | Un movimento negli ultimi N secondi della finestra la prolunga. |
| `MOTION_EVENTS` (`IsMotion,IsPeople`) | Eventi ONVIF che contano come movimento. |
| `UPLOAD_AT` (`21:00`) | Ora dopo la quale si carica il video del giorno. |
| `BUFFER_SEGMENT_SECONDS` (10) | Durata dei segmenti dell'anello. |
| `BUFFER_DIR`, `EVENTS_DIR` | Cartelle dell'anello e dei clip (`buffer`, `events`). |
| `ONVIF_HOST`, `ONVIF_PORT` (host della camera, `2020`) | Dove raggiungere gli eventi ONVIF. |

---

## Come funziona

- **Segmenti allineati all'orologio.** `-segment_atclocktime 1` taglia a 00/06/12/18; il taglio
  avviene al primo keyframe successivo, quindi le durate variano di qualche secondo.
- **Timestamp.** La camera manda pacchetti H.264 senza timestamp validi: ffmpeg usa
  `-use_wallclock_as_timestamps 1 -fflags +genpts`.
- **Audio.** L'audio della camera è PCM A-law, che l'MP4 non accetta: viene convertito in AAC
  (64 kbit/s, mono). Il video non viene toccato.
- **MP4 frammentato.** `movflags=+frag_keyframe+empty_moov+default_base_moof`. Un MP4 normale
  scrive l'indice (`moov`) solo alla fine: se ffmpeg viene ucciso il file resta illeggibile.
  Con la frammentazione ogni pezzo è già completo.
- **Titolo.** L'inizio viene dal nome del file, la fine dall'ultima scrittura: corretto anche
  per i segmenti parziali.
- **Upload.** Ripartibile a blocchi da 8 MB. Si carica solo ciò che è fermo da più di 2 minuti.
  Se la quota YouTube è finita, aspetta un'ora.
- **Playlist.** Si cerca per titolo tra le tue; se manca viene creata. L'ID è in `state.json`.
- **Log puliti.** Le credenziali della camera vengono sempre mascherate (`rtsp://***@...`),
  perché ffmpeg stampa l'URL completo negli errori. ffmpeg logga solo gli errori.

---

## Quota YouTube

Ogni upload costa circa 1.650 unità (1.600 il video, 50 l'inserimento in playlist) e la
quota giornaliera di default è **10.000**: circa **6 video al giorno**.

| Durata segmento | Upload al giorno | Rientra nella quota? |
|---|---|---|
| 6 ore (default) | 4 | sì |
| 4 ore | 6 | al limite |
| 1 ora | 24 | no, serve richiedere un aumento a Google |

Se la quota finisce lo script lo scrive nel log e riprova dopo un'ora. La quota si azzera a
mezzanotte, ora del Pacifico.

---

## Sicurezza

- Non pubblicare mai `.env`, `client_secret.json`, `token.json`. Il `.gitignore` li esclude.
- `token.json` dà accesso al tuo canale YouTube: permessi `600`. Se si perde, revoca
  l'accesso da <https://myaccount.google.com/permissions>.
- Limita il port forwarding all'IP del server e usa una porta esterna non standard.
- Usa password **diverse** per camera, account Tapo e server. Non riutilizzarle e non
  scriverle in chat o nei ticket.
- Se l'IP del server cambia, aggiorna la regola del router.

---

## Problemi comuni

| Sintomo | Causa e rimedio |
|---|---|
| `401 Unauthorized` | Utente o password dell'**account camera** sbagliati (non quelli del cloud Tapo). |
| `Operation not permitted` | Un'altra sessione sta già usando quello stream: ferma il servizio o usa `stream2`. |
| Timeout dal server | Port forwarding assente, IP del server cambiato, IP di casa cambiato e DDNS non aggiornato, oppure CGNAT. |
| `ensurepip is not available` creando il venv | Manca `python3-venv`: `sudo apt-get install python3-venv`. |
| `invalid_grant` su YouTube | Token scaduto o revocato (tipico se l'app era in Test): `auth` di nuovo e ricopia `token.json`. |
| Il pulsante "Pubblica app" è grigio | Mancano home page, privacy policy o dominio autorizzato in Branding. |
| `moov atom not found` su un file | Segmento scritto con una versione senza MP4 frammentato e interrotto: irrecuperabile, eliminalo. |
| Playlist col giorno sbagliato o tagli all'una | Il processo gira in UTC: manca `TZ` nel servizio. |
| Non vedo la playlist nel mio canale | È privata: cercala in YouTube → Tu → Playlist, non nella pagina pubblica del canale. |
| Nessun upload dopo ore | Il segmento non è ancora chiuso: guarda `recordings/` e `journalctl -u tapo-uploader`. |
| `listen` non stampa mai niente | Rilevamento spento nell'app Tapo, oppure nessun movimento: gli eventi arrivano solo ai cambi di stato. |
| Eventi ONVIF: timeout o nessuna risposta | La porta ONVIF non è raggiungibile (regola da riapplicare, IP del server cambiato). Prova `bash -c '</dev/tcp/HOST/PORTA'`. |
| La camera risponde solo alla prima richiesta ONVIF | Succede con `PullMessages` lunghi o con client HTTP standard: lo script usa richieste da 1 s su un socket semplice. |
| Eventi sempre ignorati | `MOTION_EVENTS` non contiene l'evento che la camera manda (`IsPeople` invece di `IsMotion`, o viceversa). |
| `quotaExceeded` | Troppi upload per la quota giornaliera: aumenta la durata dei segmenti. |

---

## Licenza

MIT, vedi [LICENSE](LICENSE).
