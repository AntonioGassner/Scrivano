## Ingresso

Scrivano osserva gli ingressi ({{ingressi}}) e l'archivio degli originali (`{{archivio}}`) e dice cosa è arrivato o cambiato. Le note restano in questo hub: Scrivano non scrive pagine proprie.

1. Una volta per sessione, rileva i cambiamenti e leggi la coda, che parte dal più recente:

   ```
   {{python}} "{{strumento}}/controlla.py" scan
   {{python}} "{{strumento}}/controlla.py" pending --limit 30
   ```

2. **Arrivi.** Dopo aver risposto, se negli ingressi sono comparsi file nuovi, proponi in una riga dove archiviarli secondo la mappa, al massimo una volta per sessione: {{destinazioni}}. Sposta solo dopo il sì, con un piano ([Archiviare](#archiviare)). Se l'utente li lascia dove sono, chiudi i loro eventi annotando la scelta.
3. **Archivio.** Un originale aggiunto o cambiato nell'archivio va letto e riconciliato con la nota del suo tema: aggiornala o creala, e collega l'originale senza copiarne gli identificativi. Se la nota lo collega già, perché l'ha archiviato un'altra sessione o un altro computer, chiudi l'evento con una nota.
4. Chiudi gli eventi riconciliati con una nota che dice cosa hai verificato; gli ID sono esempi:

   ```
   {{python}} "{{strumento}}/controlla.py" review 12 13 --note 'Archiviata la fattura; aggiornata la nota delle bollette'
   ```

   Se `total_pending` supera 50, dopo aver risposto riordina la coda, una volta per sessione: chiudi con `review --root ... --prefix ...` i gruppi che non toccano note, partendo da `summary`, e se un rumore si ripete proponi in una riga di escluderlo nella configurazione.

### Archiviare

Scrivi il piano in `{{strumento}}/.stato/`, con un nome nuovo per ogni piano (`archivia-AAAA-MM-GG.json`), in UTF-8. Nomina le radici di partenza e di arrivo e comincia ogni percorso dal nome della sua radice:

```json
{"radici": {"Desktop": "{{desktop}}", "Archivio": "{{archivio}}"},
 "mosse": [{"da": "Desktop/fattura.pdf", "a": "Archivio/{{cartella}}/fattura.pdf"}]}
```

```
{{python}} "{{strumento}}/riordina.py" esegui "{{strumento}}/.stato/archivia-AAAA-MM-GG.json" --prova
{{python}} "{{strumento}}/riordina.py" esegui "{{strumento}}/.stato/archivia-AAAA-MM-GG.json"
```

Lo script non sovrascrive e annota ogni mossa. Tra dischi diversi copia, verifica le impronte e solo dopo toglie l'origine; se si ferma, rilancia lo stesso piano. `annulla` riporta tutto indietro. Poi riesegui `scan`: le partenze diventano cancellazioni negli ingressi e gli arrivi aggiunte nell'archivio, da chiudere quando le note sono aggiornate.

Chiavi, `.env` e file di password non si archiviano: segnalali all'utente senza aprirli.

### Questa istanza

- Strumento: `{{strumento}}`, con `config.json` accanto a `controlla.py`; registro dove indica `database`, uno per computer.
- Radici osservate: {{radici}}.
- Modelli del {{versione}}.
