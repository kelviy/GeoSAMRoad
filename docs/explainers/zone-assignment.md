# Zone assignment

How `sentinel2data.cli assign-zones` splits the 60 zones between the annotators and picks the shared test zones that every annotator cleans.

## Core idea

Every zone gets one owner, whose cleaned label is the one published. Within each urbanisation stratum the zones are dealt out so that every annotator gets almost the same number, and among those choices the road length is balanced. One test zone per stratum, the one whose road length is nearest the stratum's median, is shared. Every annotator cleans their own copy of it, so agreement between annotators can be measured. The result depends only on the frozen baseline and the order of the annotator names, so a rerun gives the same file.

## How it works

1. **Measure each zone.** `zone_table` reads the baseline's `metadata.parquet`. It takes each kept tile's zone from its image filename, sums the length of the tile's road graph, and adds up the tiles of each zone into `road_km`, together with the split and the urbanisation class.
2. **Pick the shared zones.** `shared_zones` takes, for Urban, PeriUrban and Rural, the median `road_km` over all zones of that stratum. It picks the test zone nearest that median.
3. **Give out the shared zones.** They go to the annotators in name order, longest first, so with three annotators each owns one.
4. **Deal out the rest.** For each stratum in turn, Urban, then PeriUrban, then Rural, the other zones go out longest first. Each zone goes to the annotator with the fewest zones of that stratum. A tie goes to the one with the least road km of non-shared zones, then to whoever comes first in the name order.
5. **Write the CSV.** One row per copy someone cleans, with `zone`, `split`, `urbanisation`, `road_km`, `annotator`, `published` and `shared`. `published` is true for the owner's copy only. `shared` is true for all three copies of a shared zone, which are the three independent cleanings that agreement compares. The CLI also prints each annotator's zone counts per stratum and their copies and road km to clean in all.

## Details that matter

- **Road km is what the annotator has to look at.** The tile road graphs hold the raw labels clipped to kept tiles. On `SucculentKaroo_-30p88_18p74_Rural` their total matches the zone package's `raw_labels` clipped to its kept tiles to within 1 mm (188.4 km). Roads in dropped or untiled areas do not count, because nobody cleans them.
- **Shared zones do not count towards the km balance.** Every annotator cleans all three, so their km is the same for everyone. Counting them only for the owner would let the owner of a long shared zone take fewer other zones. On baseline v2 that made the Johannesburg owner about 2,600 km lighter than the others.
- **Name order breaks ties.** The same names in a different order give a different assignment. Agree the order once and keep it.
- **Refusals.** A stratum with no test zone, or a zone whose urbanisation class is not Urban, PeriUrban or Rural, stops the command with an error. Fewer than two names, or a repeated name, does too.
- **On baseline v2** each annotator cleans 21 to 23 copies and 21,660 to 21,743 km. The shared zones are Johannesburg (Urban, 3,627 km), AlexanderBay (PeriUrban, 215 km) and SucculentKaroo `-30p88_18p74` (Rural, 188 km).

## Why it's like this

- **Stratified by urbanisation, balanced by road length, three shared test zones near the median, the owner's copy published.** These are KEL-5 user stories 56, 57 and 59. Their stated aims are that annotator style is not confounded with zone type, and that agreement between annotators can be measured.
- **Road km over zone count.** Asking for 20 zones each as well widens the km spread on baseline v2 from 83 km to about 1,200 km. Road length tracks cleaning effort more closely than zone count, so the count is allowed to vary by one per stratum.

## Weak spots and ideas

- **Split is not stratified.** On baseline v2 the owners hold 4, 3 and 2 test zones. If annotator style matters for test scores, add split as a second stratum.
- **No Urban test zone is near the Urban median.** The median is 1,162 km and the nearest test zone, Johannesburg, is 3,627 km, so three people clean the fourth-longest zone.
- **Shared zones come from the test split only.** That is one reading of "three shared test zones" in story 57, and it is why the Urban pick is far from the median. Allowing any split would give zones nearer the median.
- **The owner's copy is both published and an agreement input.** An edit to it after the three copies are finished changes the agreement input. Agreement should be computed from a snapshot taken when all three are done.
- **Agreement is not built.** Story 58 needs the assembler first.
- **The PeriUrban pick may lack reference imagery.** Aerial tiles over AlexanderBay are partly zero-padded near the border, so some roads there cannot be judged. This was measured on the high-res product on 2026-09-21, not on the QGIS basemap. The command does not check imagery coverage.
- **Greedy, not optimal.** Longest first with a fewest-then-lightest rule is a heuristic. The spread on baseline v2 is 83 km, but nothing bounds it on other data.
- **Copies are not created.** The command only lists who cleans what. Making each annotator's separate copy of a shared zone is a manual `package-zones` run into another folder, described in the cleaning spec.

## Code map

- `cleaning/assignment.py`, `zone_table`: per-zone split, urbanisation and road km from the baseline.
- `cleaning/assignment.py`, `shared_zones`: the test zone nearest each stratum's median.
- `cleaning/assignment.py`, `assign`: owners and shared copies, one row per copy.
- `cleaning/assignment.py`, `workload`: per-annotator counts and km, printed by the CLI.
- `cli.py`, `assign_zones`: the command.
- `tests/sentinel2data/test_assign_zones.py`: CLI tests on a hand-made baseline with known road lengths.
