# C# support development evidence

## Design and development

1. Added C# discovery and member/visibility behavior tests before implementing
   the language entry. Compiler documents and executable units now come from
   Crapper, so visibility and source ownership are decided once.
2. Replaced language exclusions around the TypeScript `?? -> ||` rule with
   language profiles. Shared traversal reads each profile's binary/unary rules,
   syntax roles and workspace policy. C# does not inherit that TypeScript-only
   mutation; logical and relational pattern rules use the same extension point.
3. Separated compiler-invalid mutants from test kills. Default C# execution
   builds first and runs tests only after a successful build. Invalid outcomes
   are represented in models, EDN, reports and exit code 4, excluded from scores,
   and retried in differential runs. Custom commands use located compiler
   diagnostics; other failures retain their documented classification.
4. Copied the relative .NET build graph with private outputs. Real projects
   reproduced long-path failures, missing `build/*.props` inputs and duplicate
   ancestor `.globalconfig` settings. Copy workers now preserve build inputs,
   exclude the actual worker subtree and canonical outputs, and live outside
   the original repository. Windows path conversion stays at file-I/O boundaries.
5. A real unchanged Python worker demonstrated false kills from missing build
   dependencies. Every actual worker must now pass the unchanged test command
   before any mutation runs. Baseline failure preserves previous metrics and
   cleans the worker. Windows directory cleanup retries only transient sharing
   violations, within a bounded deadline; permanent errors remain visible.
6. An independent review reproduced basename-only coverage being assigned to two
   C# files. Mutator now uses Crapper's source-binding and line-record contract.
   A CLI run loads reports and binds sources once; the next run sees fresh input.
   Tests cover ambiguity, exact paths, unique short paths and report changes.

## Evidence and its limits

Full regression, CRAP and duplicate gates are recorded with the final revision.
Earlier all-killed self-mutation runs without a passing unchanged-worker control
were invalidated and must not be used as successful mutation evidence.

The final complete regression passes 154 tests with one platform skip. Modified
functions have CRAP below 10 (maximum 9.0198); no duplicate candidate reaches 0.8
across touched production modules. Supported changed-function mutations total
133 killed, with no remaining survivors. Each run first passes the identical
unchanged worker. The four final worker/engine/coverage/CLI files initially had
89 killed and seven survivors. Three directory-creation options were redundant
under the fresh-worker invariant and were removed. Behavioral checks killed the
remaining four: default overlay placement, refusing to link private source
ancestry, and stopping after an original-directory baseline failure. A focused
rerun killed all seven selected sites, including three previously killed sites;
it is not added wholesale to the aggregate. The other verified files contribute
40 kills (functions 8, sites 15, metrics 5, report 12).

The shared C# host was also mutated through real MSBuild and its 14 protocol
tests: 48 killed, 15 compiler-invalid, no survivors, with all source hashes
unchanged. Invalid programs are excluded from kills and scores.

Real unchanged baselines and rebuilds were checked before selected mutations:

| Project | Existing tests | Selected mutation results |
| --- | --- | --- |
| Metaheuristics.NET | 200 | 3 killed |
| Metaheuristics.NET, narrower existing class | 4 | 1 killed, 2 survived |
| v2rayN headers helper | 17 | 1 killed |
| Avalonia CornerRadiusTests | 4 | 1 killed, 1 survived |
| PowerShell WildcardPatternTests | 29 | 1 killed |

Survivors are retained as evidence about the selected test set. The experiment
projects were not edited to make the mutants pass or fail; source hashes before
and after each run match. Complete command/build/test output and cleanup checks
are kept in the workspace experiment artifacts.

runtime's selected test baseline could not be established: its official CLR
precondition build requires Visual Studio 2022 C++ tools absent on this host.
Files.Shared builds, while the repository's existing tests require interactive
GUI/Appium infrastructure. These are not successful end-to-end mutation runs.
