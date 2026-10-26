# Modalità multi-account per tapo-uploader: progetto

Data: 2026-10-26. Stato: **bozza da approvare**. Le parti segnate **[mia scelta]** sono decisioni prese da chi ha scritto il documento, non dall'amministratore: vanno controllate. Il resto è deciso dall'amministratore durante la discussione.

## 1. Perché

Oggi `tapo-uploader` serve **una** camera e **un** canale YouTube, configurati con un file `.env`. L'amministratore vuole che una decina di amici fidati possano collegare ciascuno la propria camera e caricare i video sul **proprio** canale YouTube, vedendo solo le proprie cose. Il servizio gira su un server dell'amministratore e il repository è pubblico: chiunque deve poter fare la stessa cosa sul proprio server.

## 2. Obiettivi, non-obiettivi, anti-obiettivi

**Obiettivi**
- Più utenti, ognuno con la propria camera (il numero massimo per utente è configurabile; default 1 **[mia scelta]**).
- Ogni utente carica sul proprio canale YouTube e non vede nulla degli altri.
- Una pagina personale con: video e playlist del suo canale YouTube, stato della sua camera, pulsante per collegare/scollegare YouTube.
- Una guida personalizzata per aprire la porta sul router, in due forme: istruzioni manuali e un testo già pronto da incollare in un'AI.
- Accesso configurabile: account locali, oppure Nextcloud (con ruoli presi dai gruppi di Nextcloud).
- Tutto opzionale: multi-account, login esterno e progetto Google per utente sono interruttori di configurazione.

**Non-obiettivi**
- Registrazione libera da internet, pagamenti, app per telefono, notifiche, riproduzione dei video dentro la pagina (i video sono privati: si aprono su YouTube), modifica o cancellazione dei video dalla pagina, avvio manuale delle registrazioni.
- Cambiare come funzionano oggi il rilevamento del movimento, il taglio dei clip e l'upload.

**Anti-obiettivi (anche se tutto il resto funziona, sarebbe un fallimento)**
- Un utente che vede dati, video, token o password di un altro.
- Un utente che usa il server per raggiungere reti interne (vedi SSRF, sezione 7).
- La modalità a singolo utente che smette di funzionare come oggi per chi non attiva il multi-account.
- Dipendenze o file caricati da internet a runtime nelle pagine (niente CDN: tutto copiato nel repository).

## 3. Vincoli decisi

- **Singolo utente invariato.** Senza `MULTIUSER=1` il programma si comporta esattamente come oggi (`tapo_uploader.py`, `tapo_motion.py`, `.env`).
- **Progetto Google configurabile.** Di default un progetto unico dell'amministratore (`client_secret` del server); in alternativa, per ciascun utente, un progetto proprio se l'amministratore lo permette (`ALLOW_OWN_GOOGLE_CLIENT`).
- **Login configurabile.** Nextcloud come provider esterno; i ruoli derivano dai suoi gruppi. Per l'amministratore attuale: sì Nextcloud, multi-account attivo, lui è amministratore.
- **UI:** Now UI Kit di Creative Tim (licenza MIT). Si copiano nel repository **tutti** gli asset che servono (CSS, JS, font, immagini, icone Nucleo), senza dipendenze esterne a runtime. L'avviso di copyright e la licenza MIT del kit vanno conservati nel repository (cartella dedicata con `LICENSE`). **Eccezione: il font di icone Nucleo non si copia.** Il file `nucleo-license.md` del kit vieta di ridistribuire le icone, anche gratis o dentro un file scaricabile, e un repository pubblico le ridistribuirebbe. Si usano icone SVG proprie (aggiornamento successivo alla decisione "prendi tutto", da confermare con l'amministratore).
- **Deploy dell'amministratore:** il server è raggiungibile sia dal pannello (aaPanel) sia via SSH. Il deploy resta fuori da questo documento.

## 4. Scelte tecniche **[mia scelta]**, da rivedere

- **Linguaggio:** Python, nello stesso repository.
- **Web:** Flask con template Jinja, pagine generate dal server, senza framework JavaScript e senza compilazione. Server di produzione: waitress o gunicorn dietro un reverse proxy HTTPS.
- **Database:** SQLite (un file in `DATA_DIR`), nessun servizio da installare.
- **Segreti:** password delle camere e token YouTube cifrati nel database con Fernet (libreria `cryptography`); la chiave arriva da una variabile d'ambiente (`ENCRYPTION_KEY`); senza chiave il servizio non parte.
- **OAuth:** libreria Authlib per Nextcloud; flusso web di Google per YouTube.
- **Password locali:** hash con scrypt (disponibile in `werkzeug.security`).
- **Processo:** un solo processo con il server web e un supervisore che avvia, per ogni camera attiva, gli stessi thread di oggi (registrazione, eventi ONVIF, controllore, upload giornaliero).

## 5. Il lavoro difficile: da una camera a molte

Oggi `tapo_uploader.py` e `tapo_motion.py` leggono l'ambiente a livello di modulo (`DATA`, credenziali, URL RTSP, nome del file di stato e del token). Per servire più camere serve un **contesto per camera** (cartelle `DATA/users/<id>/<camera>/`, credenziali, token, stato) passato esplicitamente alle funzioni, mantenendo il percorso a singolo utente che costruisce lo stesso contesto da `.env`. Il refactoring deve conservare il comportamento attuale e i test devono dimostrarlo.

## 6. Dati e comportamento

**Tabelle** (struttura indicativa; i dettagli sono del realizzatore):
- `users`: identificativo, origine (locale o Nextcloud), nome, ruolo (`admin` o `user`), hash password (solo locali), stato attivo.
- `cameras`: utente, nome, host, porta RTSP esterna, porta ONVIF esterna, stream, credenziali cifrate, attiva, ultimo stato, ultimo movimento, ultimo errore.
- `youtube_links`: utente, token cifrato, titolo del canale, origine del progetto Google (condiviso o proprio), eventuale client secret proprio cifrato.
- `uploads`: camera, giorno, ID video, esito, data.
- `events`: camera, ora, tipo, dettaglio (conservati per un periodo limitato).

**Ruoli da Nextcloud.** Dopo il login con Nextcloud il servizio chiede a Nextcloud i gruppi dell'utente con il suo token (endpoint `/ocs/v2.php/cloud/user`) e li confronta con la configurazione: `NEXTCLOUD_ADMIN_GROUP` (default `admin`) dà il ruolo amministratore; `NEXTCLOUD_USER_GROUP` indica chi può usare il servizio. Se `NEXTCLOUD_USER_GROUP` non è impostato, **solo gli amministratori** possono entrare (scelta prudente **[mia scelta]**). I ruoli vengono rivalutati a ogni login; chi esce dal gruppo perde l'accesso al login successivo e l'amministratore può disattivarlo subito.

**Collegamento YouTube.** Flusso OAuth di Google con `state` e PKCE, redirect configurabile. Lo scope serve per caricare video e gestire playlist. Scollegare cancella il token. La lettura di playlist e video costa circa 1 unità di quota per richiesta.

**Quota.**
- Il progetto Google condiviso ha 10.000 unità al giorno; un upload costa circa 1.650, più qualche decina per la playlist. Con un upload al giorno per ciascuno bastano per **5 o 6 utenti**, non per 10. Il documento lo dichiara apertamente.
- Il servizio tiene un conteggio stimato della quota del giorno (si azzera a mezzanotte, ora del Pacifico) e lo mostra nella pagina amministratore.
- Gli upload dei diversi utenti sono **sfalsati** nel tempo dopo `UPLOAD_AT`; il passo è configurabile **[mia scelta]**. Se la quota finisce, i clip restano e si riprova nella finestra successiva.
- Per superare il limite ci sono tre leve, tutte configurabili: richiesta di aumento a Google, upload a giorni alterni per alcuni utenti, progetto Google proprio per un utente.
- Oltre 100 utenti con un'app non verificata Google blocca nuovi collegamenti: fuori scopo, ma da segnalare nel README.

**Guida del router.** Una pagina per camera, con i valori già compilati: IP del server da autorizzare (`SERVER_PUBLIC_IP`), porte esterne e porte della camera (554 e 2020), nome DDNS suggerito. Due forme:
1. istruzioni manuali illustrate;
2. un testo da incollare in un'AI, con gli stessi valori. Non contiene segreti.
La regola da insegnare è sempre la stessa: la porta si apre **solo per l'IP del server**. Le porte esterne sono assegnate dal servizio in un intervallo configurabile e non sono prevedibili (non seriali) **[mia scelta]**. Un pulsante "Prova connessione" verifica la porta, il flusso RTSP e gli eventi ONVIF prima di attivare le registrazioni.

## 7. Sicurezza

- **SSRF.** L'utente scrive l'indirizzo della camera e il server si connette a quell'indirizzo. In multi-account il servizio deve rifiutare indirizzi privati, di loopback, link-local e dei metadati cloud, risolvendo il nome DNS e ricontrollando l'IP al momento della connessione (contro il DNS rebinding). Chi ospita su una rete locale può disattivare il controllo con un'opzione esplicita.
- **Isolamento.** Ogni accesso ai dati filtra per utente; test automatici verificano che l'utente A non legga né modifichi dati dell'utente B.
- **Sessioni e form.** Cookie `HttpOnly`, `Secure`, `SameSite=Lax`; protezione CSRF su tutti i form; limite ai tentativi di login.
- **Segreti.** Cifrati nel database, mai nei log (resta valido il mascheramento delle credenziali già presente). Le password delle camere non tornano mai nelle pagine, nemmeno all'utente.
- **Limiti.** Camere massime per utente e totali; limite di spazio disco per utente.
- **Camere esposte.** Ogni amico apre una porta sul proprio router: la guida e il README lo dicono chiaramente e raccomandano password lunghe e uniche.

## 8. Pagine

Tutte nello stile del kit, con tema chiaro/scuro **[mia scelta]**:
1. **Accesso:** locale e/o "Accedi con Nextcloud", secondo la configurazione.
2. **Pagina personale:** schede con stato della camera (raggiungibile o no, ultimo movimento, ultimo upload, clip in attesa, ultimo errore), YouTube (collega/scollega, elenco playlist "Tapo AAAA-MM-GG" e video con miniatura, titolo, data, link che apre YouTube).
3. **Camera:** aggiunta, modifica, eliminazione, guida del router, prova connessione.
4. **Amministratore:** utenti (ruolo, attivo), stato di tutte le camere, quota stimata del giorno, impostazioni principali in sola lettura.

## 9. Compatibilità e documentazione

- Nessuna modifica per chi non attiva `MULTIUSER`.
- README in inglese e in italiano: sezione multi-account, configurazione Nextcloud, guida all'onboarding delle camere, avvertenza sulla quota.
- `Dockerfile`, unità systemd e `render.yaml` aggiornati dove serve; il multi-account non è consigliato sul piano gratuito di Render (disco effimero, IP condivisi).

## 10. Test richiesti

Isolamento tra utenti; cifratura e rotazione della chiave; mappatura dei gruppi Nextcloud; controllo SSRF (IP privati, DNS rebinding); calcolo e sfalsamento della quota; il percorso a singolo utente invariato; flussi OAuth con provider simulati. L'integrazione con una vera camera resta una prova manuale.

## 11. Rischi aperti

- **Quota Google** insufficiente per 10 utenti con un solo progetto: va gestita come sopra.
- **Dieci camere esposte** su internet: il rischio ricade sui router degli amici, non sul server.
- **IP del server fisso:** se cambia, tutti devono aggiornare la regola.
- **Icone Nucleo:** escluse dal repository pubblico perché la loro licenza vieta la ridistribuzione; si usano icone SVG proprie.
- **Guida del router:** i router sono molti; per questo esiste il testo da incollare in un'AI.

## 12. Chiamate emerse rileggendo il documento **[mia scelta]**

- **Indirizzo pubblico del servizio.** Serve una variabile `PUBLIC_URL` (HTTPS) per costruire i redirect di Google e di Nextcloud.
- **Primo amministratore senza Nextcloud.** Si crea con un comando (`python -m tapo_web create-admin`) o con `ADMIN_USERNAME`/`ADMIN_PASSWORD` al primo avvio.
- **Utente che esce dal gruppo di Nextcloud o viene disattivato.** Le sue camere si fermano subito; i suoi dati restano 30 giorni (configurabile) e poi vengono cancellati, token inclusi.
- **Chi scollega YouTube.** Le registrazioni continuano a creare i clip, ma l'upload resta in attesa; dopo 7 giorni senza collegamento i clip più vecchi vengono scartati per non riempire il disco.
- **Registrazione dell'app su Nextcloud.** L'amministratore crea a mano il client OAuth2 su Nextcloud (Sicurezza, Client OAuth 2.0) e inserisce l'ID e il segreto nella configurazione; il documento di installazione lo spiega.

## 13. Cosa resta al realizzatore

Struttura esatta delle tabelle e delle cartelle; scelta tra waitress e gunicorn; passo dello sfalsamento degli upload; formato del file di configurazione oltre alle variabili d'ambiente; testi e illustrazioni della guida; ordine di costruzione (il refactoring del contesto per camera viene per primo).
