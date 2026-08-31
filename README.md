# Przydział brygad — zakłady i przewoźnicy WTP

https://mccartney.github.io/przydzial-brygad/

Codziennie budowana strona: jedna tabela, wiersz na linię autobusową, kolumny **Dzień
powszedni** i **Sobota / niedziela i święta**, a w komórce brygady pogrupowane po
obsługującym je zakładzie (`R-1: 1, 5, 8, 10, 13-14, 017 · R-2: 2-4, 6-7, …`).

## Skąd to się bierze

Źródłem jest [WarsawGTFS](https://mkuran.pl/gtfs/) (`warsaw.zip`, przebudowywany
codziennie ok. 00:15 UTC). Zakład **nie jest** polem w GTFS — trzeba go wyliczyć:

1. **Wybór dnia.** `service_id` ma postać `2026-09-08:PcS`. Dla każdego typu dnia
   (`PcS` pon.–czw., `PtS` piątek, `SbS` sobota, `NdS` niedziela i święta) bierzemy serwis
   o najwyższym prefiksie daty — najdalej wysuniętą w przyszłość edycję rozkładu.
   `calendar_dates.txt` powtarza te serwisy tygodniowo do końca miesiąca, więc liczy się
   prefiks, a nie najpóźniejsza data kursowania.
2. **Kursy techniczne.** Przejazdy z `exceptional=1` (warianty `TD-*` wyjazd, `TZ-*` zjazd)
   zaczynają się lub kończą na przystankach zajezdni: `R-1 Zajezdnia Woronicza`,
   `R-2 Kleszczowa`, `R-3 Ostrobramska`, `R-4 Stalowa`, `R-6 Płochocińska`.
3. **Propagacja po `block_id`.** Zajezdnia z dowolnego kursu technicznego rozlewa się na
   cały całodzienny łańcuch pojazdu, więc brygada bez własnego zjazdu dziedziczy ją
   z innej linii obsługiwanej tym samym autobusem.
4. **Scalenie dni.** Kolumna to suma brygad z dwóch typów dnia. Brygada kursująca tylko
   w części z nich dostaje górny indeks (`030-032`<sup>pt</sup> = tylko piątek).

Wydział Włościańska pomijamy, gdy brygada ma też zjazd do zajezdni — to postój zewnętrzny
R-4/R-6 (autobus wyjeżdża rano ze Stalowej, a nocuje na Włościańskiej), więc mówi gdzie
pojazd nocuje, a nie kto go obsługuje. Nigdy nie występuje jako jedyny sygnał.

## Czego tu nie ma

**Przewoźnicy kontraktowi nie publikują kursów technicznych.** Linie obsługiwane przez
Mobilis, PKS Grodzisk i ReloBus mają w GTFS `exceptional=0` dla wszystkich przejazdów,
a ich zajezdnie nie są przystankami — takie brygady wychodzą jako **nieznany**. To ok. 18%
brygad; 33 linie (m.in. 103, 112, 133, 183, 190, 250, 710, 900, N42, N50) są puste w całości.
Numeracja brygad nie pomaga: na 189 zarówno `1-16`, jak i `017-028` to MZA, a na 719
brygady `1-5` i `06-09` są spoza MZA.

Da się to domknąć poza GTFS: [`warsaw/vehicles.pb`](https://mkuran.pl/gtfs/warsaw/vehicles.pb)
(GTFS-RT, bez klucza API) ma encje `V/<linia>/<brygada>` z `vehicle.label` = numerem
taborowym, a [baza pojazdów ZTM](https://www.ztm.waw.pl/baza-danych-pojazdow/) mapuje numer
na przewoźnika i zajezdnię — tak robi to
[numeracja-autobusow](https://github.com/mccartney/numeracja-autobusow). Wymagałoby to
akumulowania obserwacji z wielu godzin i dotyczyłoby „dziś", nie przyszłego rozkładu,
więc na razie tego nie robimy.

Pomijamy też linie podmiejskie `L-*` — obsługują je przewoźnicy gminni, o których nie wie
ani GTFS, ani baza pojazdów ZTM.

## Uruchomienie

```
python3 build.py               # pobiera warsaw.zip (110 MB), jeśli go nie ma obok
python3 build.py --feed /ścieżka/warsaw.zip
```

Bez zależności, sama biblioteka standardowa; przebieg trwa ~6 s. Wynik to `przydzial.html`
(publikowany jako `index.html`) oraz `brygady.json` — maszynowy zrzut, jednocześnie
awaryjne źródło: jeśli świeży feed dałby mniej niż 200 linii albo pokrycie zakładów poniżej
60%, skrypt publikuje ostatnią zacommitowaną wersję zamiast zepsutej strony. `brygady.json`
odświeżamy commitem ręcznie — CI go nie zapisuje z powrotem.

## Źródła danych i licencje

- [Zarząd Transportu Miejskiego w Warszawie](https://ztm.waw.pl)
- [GTFS: Mikołaj Kuranowski](https://mkuran.pl/gtfs/)
- [Kształty tras: © OpenStreetMap (ODbL)](https://www.openstreetmap.org/copyright)
