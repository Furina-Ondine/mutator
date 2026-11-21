# mutator

Mutation testing for Clojure, Java, Go, TypeScript, Rust, Python, and C#. One run detects the language of each source file, applies that language's mutation rules, and writes the snapshot [uml-viewer](https://github.com/unclebob/uml-viewer) already reads.

Clojure follows [clj-mutate](https://github.com/unclebob/clj-mutate). Go follows [mutate4go](https://github.com/unclebob/mutate4go). Java follows [mutate4java](https://github.com/unclebob/mutate4java). TypeScript, Rust, and Python use the same decisions as Java, spelled in that language. Function names and namespaces come from [crapper](https://github.com/unclebob/crapper), which is what uml-viewer joins to `.metrics/crap.edn`.

A mutant survives when the tests still pass. For Java, Go, Rust, and C#, a
reported test failure kills the mutant and a compiler diagnostic for project
source marks it invalid. A failed run without either kind of evidence stops the
file and preserves its previous snapshot. Uncovered and invalid sites are
excluded from the score, killed / (killed + survived).

## Run

Python 3.11 or later is required. Linux and macOS use the shell launcher:

```bash
./mutator
```

Windows uses PowerShell 7.3 or later (`pwsh`):

```powershell
.\mutator.ps1 --help
```

To analyze another project from the tool checkout, use `--root`:

```powershell
.\mutator.ps1 --root ..\my-project --no-coverage --max-workers 3 src\demo.py
```

The first run creates `.venv` and installs the tool. crapper has to be checked out next to this directory (`../crapper`), because that is where the namespace and function names come from. From a project root the tool walks the tree, skips `test`, `spec`, `vendor`, `node_modules`, and `target`, and writes `.metrics/mutate/<namespace>.edn`.

Both launchers keep the virtualenv in the tool checkout and preserve the
project's working directory. Installation logs go to stderr. CLI output and
snapshot files use UTF-8. The project's test and coverage tools must be
installed; Windows batch entry points such as `npm.cmd` and `mvn.cmd` are
resolved through `PATH`.

The first run of a tree executes every covered mutant. Start with the file you are working on.

```bash
./mutator src/demo/core.clj          # one file, or a directory
./mutator --scan src/demo           # list sites, do not run tests
./mutator --changed                 # git additions and edits
./mutator --mutate-all              # rerun killed mutants too
./mutator --no-coverage             # do not skip uncovered lines
./mutator --use-existing-coverage   # read reports already on disk
./mutator --test-command "pytest -q tests/test_demo.py"
./mutator --max-workers 3 src/demo/core.clj
```

Once a snapshot exists, the next run is differential. It reruns survivors,
invalid sites and every site in a function whose text changed. Verified kills
from unchanged functions are kept. Earlier snapshots with unverified outcomes
are refreshed when their source files run again. `--since-last-run` is that
same selection. `--mutate-all` ignores it.

Exit `0` when every executed mutant was killed or none ran. Exit `2` on a
baseline or execution failure, `3` when a mutant survives, and `4` if a
mutant is invalid.

## Workers

Mutants of one file run at the same time. The default is one worker per core. `--max-workers` sets the cap. The run uses the smaller of that cap, the number of cores, and the number of selected sites. Files are still taken one at a time.

Java, Go, Rust, C#, and TypeScript use private workspace copies outside the
original project. Each worker keeps the relative project layout and its
own build inputs and outputs. Include the relevant projects in `--root`
and use project-relative paths to write outputs into the worker. A test
command with a hardcoded external output path still writes to that path.
Other languages use the original overlays under `target/mutation-workers`.
Windows workers support copying without symlink privileges.

The original checkout and every private worker must pass the same test
command before mutations start. Mutants run in the corresponding worker
directory. The worker is removed after its file finishes. A copy left under
`target/mutator-backup/` by an interrupted older run is restored before a
non-scan run.

Timeouts terminate the test process and its descendants before worker cleanup.
Windows uses a native Job Object, including when a test's parent process has
already exited. Linux and macOS use a process group. Mutation replacements
preserve the source file's UTF-8 bytes and line endings.

## Snapshot

uml-viewer loads every `*.edn` file under `.metrics/mutate`. Each file is one namespace. A Java file with an inner class, or a Go file with a function and a method, therefore writes more than one file. Two Go files in the same package share one snapshot, and rerunning one of them keeps the other's forms.

```clojure
{:version 1
 :source "src/demo/board.py"
 :namespace "demo.board.Board"
 :outcomes {"[\"src/demo/board.py\",\"demo.board.Board\",\"defn/place\",10,11,\">\",\">=\"]" :survived}
 :forms [{:id "defn/place"
          :kind "defn"
          :file "src/demo/board.py"
          :line 2
          :end-line 4
          :hash "…"
          :killed 1
          :survived 1
          :uncovered 0
          :sites 2}
         {:id "defn-/_hide"
          :kind "defn-"
          :killed 0
          :survived 0
          :uncovered 1
          :sites 1}]}
```

`:namespace` is the class key crapper uses. The form id is what the viewer splits:

| Form id | Operation | Private |
| --- | --- | --- |
| `defn/place` | `place` | no |
| `defn-/hide` | `hide` | yes |

`:sites` is every mutation in the function, including uncovered ones. The viewer prints `---no mutation sites---` when that count is zero.

| Language | `:namespace` | `:name` |
| --- | --- | --- |
| Clojure | the `ns` | the `defn` / `defn-` name |
| Java | `package.Class`, or `package.Outer.Inner` | the method name |
| Go | the package import path, or `import/path.Receiver` | the function or method name |
| TypeScript | the dotted module path, or `module.Class` | the function or method name |
| Rust | `crate::module`, or `crate::module::Type` | the function or method name |
| Python | the dotted module path, or `module.Class` | the function or method name |

`defn-` is Clojure's private form. Elsewhere a private method, an unexported Go function, a Python name that starts with `_`, a Rust function that is not `pub`, or a TypeScript `private` / `#` name uses `defn-/`.

## What each language mutates

Tokens inside strings, comments, and quoted Clojure forms are left alone.

| Language | Mutations |
| --- | --- |
| Clojure | clj-mutate's head symbols: `+`/`-`, `*` to `/`, `inc`/`dec`, comparisons, `=`/`not=`, `if`/`if-not`, `when`/`when-not`, `and`/`or`, and the seq, predicate, and coercion pairs. `true`/`false` and `0`/`1` anywhere. |
| Java | `true`/`false`, `==`/`!=`, `<`/`<=`, `>`/`>=`, `+`/`-`, `*`/`/`, `&&`/`||`, delete `!` and unary `-`, `0`/`1`. Constructors are not entries, matching crapper. |
| Go | mutate4go: `+`/`-`, `*` to `/` only, comparisons, `==`/`!=`, `true`/`false`, `&&`/`||`, `0`/`1`. |
| TypeScript | the Java set, plus `===`/`!==`, `??` to `||`, and `?.` to `.`. A call or index drops `?.` (`a?.()` becomes `a()`). `.js`, `.jsx`, `.mjs`, and `.cjs` use these rules. |
| Rust | the Java set (`&&`/`||`, `!`, unary `-`). |
| Python | the Java set, spelled `and`/`or`, `not`, and `True`/`False`. |
| C# | arithmetic, comparison, Boolean and pattern operators, unary negation, and `0`/`1` in shared Roslyn member spans. |

## Tests and coverage

The baseline command has to pass before any mutant runs. A mutant's timeout is ten times that baseline, and at least two seconds. `--timeout-factor` changes the multiple. `--test-command` replaces the default. Defaults:

| Language | Command | Where |
| --- | --- | --- |
| Clojure | `clj -M:spec --tag ~no-mutate` when Speclj is on the classpath, otherwise `clj -M:test` or `bb spec` | nearest `deps.edn` or `bb.edn` |
| Java | `mvn -q test -DexcludeTags=no-mutate` | nearest `pom.xml` |
| Go | `go test -json -count=1` of the file's package | nearest `go.mod` |
| TypeScript | `npm test` | nearest `package.json` |
| Rust | `cargo test --message-format=json` | nearest `Cargo.toml` |
| Python | the project's `.venv` or `venv` Python running `pytest`, or `unittest discover` | nearest project file |
| C# | `dotnet test` | project root |

Coverage is generated with crapper's commands unless `--use-existing-coverage` or `--no-coverage` is set. A file missing from the report is treated as uncovered, and the run says so.
Languages that use LCOV can also read Cobertura reports. LCOV takes precedence
when both formats cover the same file.

C# uses the companion Crapper with Roslyn support and the .NET 10 SDK.
Pass `--use-existing-coverage` or `--coverage-command` with your C# collector.
When supplying `--test-command` for a compiled language, keep its test
and compiler diagnostics in the output. TypeScript uses the project's test
script and its exit status.

Python environments use `Scripts/python.exe` on Windows and `bin/python` on
Linux and macOS. Generated commands preserve argument boundaries. User-written
`--test-command` and `--coverage-command` strings use PowerShell 7.3+ on Windows and
the system shell on Linux and macOS, so use the corresponding shell's quoting.
PowerShell commands preserve explicit exit codes and the last native command's
exit code; PowerShell errors return a nonzero status.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

On Windows:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pytest -q
```

The test workflow covers Windows, Linux, and macOS with Python 3.11 and 3.14.
