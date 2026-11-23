
#         _.-'-``-._           ______  ______   ______   ______  _______ _______  ______  81
#       ,'::::::''  `.        | |     | |  | \ | |  \ \ | |  | |   | |     | |   | |  | |  
#      ::::::'        :       | |---- | |__| | | |  | | | |__| |   | |     | |   | |__| |   
#      |:::'          |       |_|____ |_|  \_\ |_|  |_| |_|  |_|   |_|     |_|   |_|  |_|   
#      ;:::  .  ,     :
#     :-------::-------:
#     `.__O__.;:.__O__.'
#     (.:::::(:_)_____.)

# LED Test — verifica colore e accensione di una barra LED via webcam

Legge la barra LED con una telecamera USB, misura il colore di ogni LED
(tinta HSV) e verifica che siano **tutti accesi** e del colore atteso.
Esce con `0` = PASS, `1` = FAIL, quindi si integra in uno script di collaudo.

## Installazione

Il progetto usa un ambiente virtuale in `.venv` (Python 3.13 a 64 bit), gia creato.
Se devi ricrearlo da zero:

```bash
py -3.13 -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Da terminale usa `.venv\Scripts\python.exe led_test.py ...`, oppure attiva
l'ambiente con `.venv\Scripts\Activate.ps1` e poi `python led_test.py ...`.

### VS Code

`.vscode/settings.json` punta all'interprete del venv: se VS Code segnala
`ModuleNotFoundError: No module named 'cv2'` significa che sta usando un altro
Python (sulla macchina ce ne sono quattro). Premi `Ctrl+Shift+P` >
**Python: Select Interpreter** e scegli quello dentro `.venv`, poi riapri il
terminale integrato. In `.vscode/launch.json` ci sono le configurazioni di
debug pronte (elenco telecamere, ROI, live, test, selftest).

## Interfaccia grafica (modo consigliato)

Doppio clic su `avvia_gui.bat`, oppure:

```bash
.venv\Scripts\python.exe led_gui.py
```

In VS Code: `Ctrl+Shift+D` e scegli **LED: interfaccia grafica**.

A sinistra l'anteprima live con le zone di lettura e il colore riconosciuto in
ognuna; a destra due schede.

### Scheda 1 - Setup

1. **Telecamera**: **Cerca** elenca le webcam collegate, scegli l'indice e premi
   **Collega**. Se non la trova, cambia backend (`msmf`).
2. **Esposizione**: abbassa il cursore finche i LED non sono piu bruciati (bianchi).
3. **Zone di lettura**: imposta in **Numero di LED** quanti LED ha il pezzo, poi
   **trascina col mouse sull'anteprima una zona alla volta**, nell'ordine dei LED.
   Le zone sono indipendenti: puoi metterle dove vuoi, anche su LED sparsi e non
   allineati.
   - clic su una zona: la seleziona (riquadro giallo, evidenziata nell'elenco)
   - frecce: sposta la zona selezionata - Shift+frecce: la ridimensiona
   - `Canc` o **Elimina**: cancella la selezionata (le altre si rinumerano)
   - **Elimina ultima** / **Cancella tutte**
   - **Griglia automatica da un rettangolo**: se i LED sono allineati e
     regolari, trascini un rettangolo su tutta la barra e ottieni le N zone
     gia distribuite, da ritoccare a mano se serve.

   Il contatore accanto alla casella mostra `zone: 3 / 8` in rosso finche il
   numero di zone non corrisponde ai LED dichiarati; in quel caso il test
   fallisce di proposito, indicando quante zone mancano.
4. **Colori attesi**: solo accensione, tutti dello stesso colore, oppure una
   sequenza tipo `R,G,B` che segue **l'ordine in cui hai creato le zone** e si
   ripete se piu corta.
5. **Soglie**: normalmente si lasciano come sono. **Calibra bianco**, con un
   riferimento bianco (LED bianco oppure R+G+B accesi), corregge la dominante
   della webcam usando i pixel delle zone definite.

**Salva impostazioni** scrive tutto in `config.json` (zone comprese): al
riavvio ritrovi la configurazione, e la usa anche la riga di comando.

### Scheda 2 - Test

Metti il pezzo in posizione e premi **ESEGUI TEST** (o `F5`). Il programma media
5 frame, congela il risultato e mostra PASS/FAIL in grande, la tabella LED per
LED (colore letto, atteso, H/S/V, esito - righe rosse per i KO) e l'elenco delle
anomalie. I contatori tengono il totale PASS/FAIL della sessione;
**Salva report...** scrive un JSON piu l'immagine annotata, e la spunta
*salva ogni test in reports/* archivia automaticamente ogni prova.

## Uso da riga di comando

1. Individua la telecamera:

```bash
python led_test.py list
```

2. Con la barra LED accesa, seleziona una volta l'area inquadrata (si salva in `config.json`):

```bash
python led_test.py roi --cam 0
```

3. Regola l'esposizione guardando l'anteprima — i LED **non devono essere bruciati**
   (se compare l'avviso "sovraesposto", abbassa il valore):

```bash
python led_test.py live --leds 8 --exposure -6
```

4. Calibra il bilanciamento del bianco con un riferimento bianco (LED bianco, oppure R+G+B accesi insieme):

```bash
python led_test.py wb
```

5. Test di collaudo:

```bash
python led_test.py test --leds 8 --expect-all G --json report.json --save esito
```

```bash
python led_test.py test --leds 3 --expect R,G,B
```

`--expect` accetta anche un pattern che si ripete: `--expect R,G,B` su `--leds 9`
verifica R,G,B,R,G,B,R,G,B. Codici colore: `R G B Y C M W`.
Con `--leds 0` i LED accesi vengono individuati automaticamente come blob luminosi
(comodo se non conosci il numero, ma meno ripetibile di un conteggio fisso).

Senza `--expect` viene comunque verificato che ogni LED sia acceso e con
luminosità in linea con gli altri.

## Cosa viene controllato

| Controllo | Come |
|---|---|
| LED spento | V medio del segmento sotto `off_value` |
| LED debole / semi-guasto | V sotto `dim_ratio` × mediana degli altri |
| Colore errato | tinta HSV fuori dall'intervallo del colore atteso |
| Bianco / canale mancante | saturazione sotto `min_sat` → "bianco" |
| Esposizione sbagliata | quota di pixel saturi oltre `clip_warn` |
| Zone mancanti | numero di zone diverso dai LED dichiarati |
| Zona fuori inquadratura | zona che cade fuori dall'immagine |

Le soglie stanno in `config.json` (create dal primo comando `roi`) e si possono
ritoccare a mano.

## Verifica senza telecamera

`selftest.py` genera barre LED sintetiche (colori corretti, LED spento, LED
debole, colore sbagliato, zone manuali, zone mancanti) e controlla che il
verdetto sia quello atteso:

```bash
.venv\Scripts\python.exe selftest.py
```

`selftest_gui.py` apre la finestra vera e simula col mouse l'aggiunta delle
zone, la selezione, lo spostamento, l'eliminazione e l'esecuzione del test:

```bash
.venv\Scripts\python.exe selftest_gui.py
```

## Note di banco

- **Blocca esposizione e bilanciamento del bianco.** Se la webcam li regola da
  sola, lo stesso LED misura colori diversi da un frame all'altro. Il programma
  prova a disattivarli; su Linux/Raspberry si forzano con `v4l2-ctl`.
- **Non saturare il sensore.** Un LED sovraesposto diventa bianco e la tinta non
  è più leggibile: allontana la telecamera, riduci l'esposizione oppure metti un
  filtro ND o un diffusore davanti alla barra.
- **Geometria fissa.** Telecamera e barra in posizione ripetibile (staffa o
  dima), così la ROI resta valida fra un pezzo e l'altro.
- **Luce ambiente costante**, preferibilmente bassa: il LED deve essere la
  sorgente dominante.
- **LED in PWM**: il comando `test` media più frame (`--frames`) proprio per
  smussare lo sfarfallio.
