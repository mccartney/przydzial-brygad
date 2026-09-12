# Przydział brygad — zakłady i przewoźnicy WTP

https://mccartney.github.io/przydzial-brygad/

Codziennie budowana strona: jedna tabela, wiersz na linię autobusową, kolumny **Dzień
powszedni** i **Sobota / niedziela i święta**, a w komórce brygady pogrupowane po
obsługującym je zakładzie (`R-1: 1, 5, 8, 10, 13-14, 017 · R-2: 2-4, 6-7, …`).
Bez linii podmiejskich `L-*`.

Zakład bierzemy wprost z pola `depot_id` w `trips.txt` — obejmuje ono zarówno zajezdnie
MZA, jak i przewoźników kontraktowych (Mobilis, PKS Grodzisk, Relobus, KMŁ).

`python3 build.py` buduje `przydzial.html` (publikowany jako `index.html`) oraz
`brygady.json` — maszynowy zrzut, jednocześnie awaryjne źródło, gdyby świeży feed okazał
się zepsuty. Bez zależności, sama biblioteka standardowa.

## Źródła danych i licencje

- [Zarząd Transportu Miejskiego w Warszawie](https://ztm.waw.pl)
- [GTFS: zbiorkom.live](https://zbiorkom.live) — `https://cdn.zbiorkom.live/gtfs/warsaw.zip`
