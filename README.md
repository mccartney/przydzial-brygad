# Przydział brygad — zakłady i przewoźnicy WTP

https://436.pl/przydzial-brygad/ (także https://mccartney.github.io/przydzial-brygad/)

Codziennie budowana strona: karta na linię autobusową lub tramwajową, a w niej brygady
pogrupowane po obsługującym je zakładzie (`R-1: 1, 5, 8, 10, 13, 14, 017 · R-2: 2, 3, 4, 6, 7, …`).
Bez linii lokalnych `L-*`, metra i SKM.

Na górze przełącznik **autobusy / tramwaje** (domyślnie autobusy, wybór zapamiętywany
w przeglądarce; link do zajezdni tramwajowej, np. `#Wola`, sam przełącza na tramwaje). Zajezdnie tramwajowe mają nazwy zamiast numerów
(`Wola`, `Praga`, `Mokotów`, `Żoliborz`, `Annopol`), bo numeracja R-1…R-5 Tramwajów
Warszawskich koliduje z numeracją zajezdni MZA. Przy tramwajach typ taboru to wprost
`fleet_type` z feedu (`120N`, `DUO`, `2w` = dwa wagony).

Nad kartami wybór dnia, domyślnie dziś. Obejmuje dni od dziś do najnowszej edycji rozkładu
w feedzie (ok. tygodnia) — dalsze daty feed tylko powtarza z poprzednich tygodni, więc ich
nie pokazujemy. Kolejne dni z identycznym przydziałem w całej sieci są złączone w jeden
przycisk, np. `wt–czw 3–5 listopada`; święto w dzień roboczy jest oznaczone
(`śr 11 listopada rozkład świąteczny`).

Pod kartami lista zmian przydziału w horyzoncie feedu (ok. tygodnia do przodu), np.
`116/01 DP od 12.10: R-1 → R-2` albo `116/M6 DŚ od 10.10: likwidacja`. Każda edycja
rozkładu jest porównywana z tym samym dniem tygodnia tydzień wcześniej (żeby brygada
kursująca np. tylko w czwartki nie „znikała” co tydzień), a zmiana datowana od pierwszego
dnia, od którego nowy stan obowiązuje bez przerwy. Lista ma dwie zakładki: **wg linii** (jak
wyżej) i **wg zakładu / przewoźnika** — blok na każdy zakład lub przewoźnika z tym, co
przejmuje (`116/01 DP od 12.10: z R-1`) i co oddaje (`… do R-2`, `likwidacja`); zmiana między
dwoma zakładami pojawia się w obu blokach. Brygada obsługiwana jednym pojazdem na kilku liniach
(ten sam `block_id` w feedzie) jest w liście zmian jedną pozycją, np. `411+502 / 721, 722`,
a nie osobno dla każdej linii — w kartach linii nadal widać ją przy każdej z nich.
Brygada, która zmienia tylko zapis numeru z zerem na początku lub bez niego (`6` ↔ `06`,
ta sama linia, data, typ dnia i zakład), nie jest pokazywana jako likwidacja i nowa brygada,
tylko w osobnej liście **zmiany brygad**, np. `739/6 → 06 DP od 1.12: R-1`. Gdy zmienia się
też zakład (`220/08` Mobilis → `220/8` R-2), zostaje likwidacją i nową brygadą.

Zakład bierzemy wprost z pola `depot_id` w `trips.txt` — obejmuje ono zarówno zajezdnie
MZA, jak i przewoźników kontraktowych (Mobilis, PKS Grodzisk, Relobus, KMŁ) oraz zajezdnie
Tramwajów Warszawskich.

Pozycje legendy są klikalne: wybór zakładu lub przewoźnika zostawia tylko jego brygady
(karty i lista zmian; da się go podlinkować, np. `#Relobus`). Mobilis i Relobus mają po
kilka kodów `depot_id`, po jednym na umowę (`Mob12`, `Mob88`, `Relobus29/38/49/58`); po
wybraniu przewoźnika jego brygady w kartach są rozpisane na te kody, np. `Relobus 38` /
`Relobus 58`. Lista zmian zostaje na poziomie przewoźnika.

`python3 build.py` buduje `przydzial.html` (publikowany jako `index.html`) oraz
`brygady.json` — maszynowy zrzut, jednocześnie awaryjne źródło, gdyby świeży feed okazał
się zepsuty. Bez zależności, sama biblioteka standardowa.

## Źródła danych i licencje

- [Zarząd Transportu Miejskiego w Warszawie](https://ztm.waw.pl)
- [GTFS: mccartney/WarsawGTFS](https://github.com/mccartney/WarsawGTFS) — `https://436.pl/gtfs/warsaw.zip`
