# Przydział brygad — zakłady i przewoźnicy WTP

https://436.pl/przydzial-brygad/ (także https://mccartney.github.io/przydzial-brygad/)

Codziennie budowana strona: jedna tabela, wiersz na linię autobusową, kolumny **Dzień
powszedni** i **Sobota / niedziela i święta**, a w komórce brygady pogrupowane po
obsługującym je zakładzie (`R-1: 1, 5, 8, 10, 13, 14, 017 · R-2: 2, 3, 4, 6, 7, …`).
Bez linii lokalnych `L-*`.

Pod kartami lista zmian przydziału w horyzoncie feedu (ok. tygodnia do przodu), np.
`116/01 DP od 12.10: R-1 → R-2` albo `116/M6 DŚ od 10.10: likwidacja`. Każda edycja
rozkładu jest porównywana z tym samym dniem tygodnia tydzień wcześniej (żeby brygada
kursująca np. tylko w czwartki nie „znikała” co tydzień), a zmiana datowana od pierwszego
dnia, od którego nowy stan obowiązuje bez przerwy. Karty pokazują stan po wszystkich zmianach.

Zakład bierzemy wprost z pola `depot_id` w `trips.txt` — obejmuje ono zarówno zajezdnie
MZA, jak i przewoźników kontraktowych (Mobilis, PKS Grodzisk, Relobus, KMŁ).

`python3 build.py` buduje `przydzial.html` (publikowany jako `index.html`) oraz
`brygady.json` — maszynowy zrzut, jednocześnie awaryjne źródło, gdyby świeży feed okazał
się zepsuty. Bez zależności, sama biblioteka standardowa.

## Źródła danych i licencje

- [Zarząd Transportu Miejskiego w Warszawie](https://ztm.waw.pl)
- [GTFS: mccartney/WarsawGTFS](https://github.com/mccartney/WarsawGTFS) — `https://436.pl/gtfs/warsaw.zip`
