# mutator

Mutation testing for Clojure, Java, C#, Go, TypeScript, Rust, and Python. One run detects the language of each source file, applies that language's mutation rules, and writes the snapshot [uml-viewer](https://github.com/unclebob/uml-viewer) already reads.

Clojure follows [clj-mutate](https://github.com/unclebob/clj-mutate). Go follows [mutate4go](https://github.com/unclebob/mutate4go). Java follows [mutate4java](https://github.com/unclebob/mutate4java). TypeScript, Rust, and Python use the same decisions as Java, spelled in that language. Function names and namespaces come from [crapper](https://github.com/unclebob/crapper), which is what uml-viewer joins to `.metrics/crap.edn`.

A mutant is killed when the tests fail or time out. It survives when the tests still pass. A C# mutant rejected by the build is invalid and contributes no test kill. A site on a line the coverage report does not hit is uncovered and is not run. The score uml-viewer paints is killed / (killed + survived), excluding invalid mutants.

## Run

Python 3.11 or later is required. C# analysis also requires the project's .NET SDK on `PATH`; crapper builds its Roslyn host in a temporary cache using that SDK. Linux and macOS use the shell launcher:

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

Once a snapshot exists, the next run is differential. It reruns survivors, invalid mutants, and every site in a function whose text changed. Killed mutants in an unchanged function stay killed. `--since-last-run` is that same selection. `--mutate-all` ignores it.

Exit `0` when every executed mutant was killed. Exit `2` when the baseline tests fail. Exit `3` when a mutant survives. Exit `4` when a mutant is invalid; this takes priority over survivors. A failed baseline does not rewrite the snapshot.

## Workers

Mutants of one file run at the same time. The default is one worker per core. `--max-workers` sets the cap. The run uses the smaller of that cap, the number of cores, and the number of selected sites. Files are still taken one at a time.

Each worker is a directory under `target/mutation-workers`. The file being mutated is a private copy. A module that reaches that file through a relative import is copied too, so Node resolves the import to the private copy. The rest of the tree is linked where symlinks are available. Otherwise, dependencies are copied into each worker, excluding version control, virtualenvs, build outputs, and caches. Windows does not require administrator privileges or Developer Mode. Copying uses more disk space and setup time; `--max-workers` controls the number of concurrent copies. The original files stay as they are. The overlay is removed when that file's mutants finish.

The baseline still runs once, in the real tree, before any worker starts. The mutant command then runs inside the worker, in the same directory it would have used in the real tree, so a relative path such as `src` or `./pkg` refers to the overlay. A copy left under `target/mutator-backup/` by an interrupted older run is restored before a non-scan run.

C# workers copy the project's build graph, including project references and
`Directory.Build.*` files, into private directories. They omit `bin`, `obj`,
`TestResults`, `.artifacts`, `.vs`, `BenchmarkDotNet.Artifacts`, and the worker
tree, following crapper's source-discovery exclusions. Each mutant rebuilds against its own source
and outputs; original source files and build outputs are preserved.

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
          :invalid 0
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

`:sites` is every mutation in the function, including uncovered and invalid ones. `:outcomes` can record `:killed`, `:survived`, or `:invalid`; the optional `:invalid` count is zero in older snapshots. The viewer prints `---no mutation sites---` when the site count is zero.

| Language | `:namespace` | `:name` |
| --- | --- | --- |
| Clojure | the `ns` | the `defn` / `defn-` name |
| Java | `package.Class`, or `package.Outer.Inner` | the method name |
| C# | full namespace and nested type | method, constructor, operator, or accessor name; top-level statements use `Program` / `<top-level>` |
| Go | the package import path, or `import/path.Receiver` | the function or method name |
| TypeScript | the dotted module path, or `module.Class` | the function or method name |
| Rust | `crate::module`, or `crate::module::Type` | the function or method name |
| Python | the dotted module path, or `module.Class` | the function or method name |

`defn-` is Clojure's private form. Elsewhere a private method, an unexported Go function, a Python name that starts with `_`, a Rust function that is not `pub`, or a TypeScript `private` / `#` name uses `defn-/`.

## What each language mutates

Tokens inside strings, comments, and quoted Clojure forms are left alone. C# uses crapper's Roslyn document and native compiler syntax roles; it rejects syntax errors before selecting mutations. Other languages use the shared Tree-sitter document and their existing mutation rules.

| Language | Mutations |
| --- | --- |
| Clojure | clj-mutate's head symbols: `+`/`-`, `*` to `/`, `inc`/`dec`, comparisons, `=`/`not=`, `if`/`if-not`, `when`/`when-not`, `and`/`or`, and the seq, predicate, and coercion pairs. `true`/`false` and `0`/`1` anywhere. |
| Java | `true`/`false`, `==`/`!=`, `<`/`<=`, `>`/`>=`, `+`/`-`, `*`/`/`, `&&`/`||`, delete `!` and unary `-`, `0`/`1`. Constructors are not entries, matching crapper. |
| C# | `true`/`false`, `==`/`!=`, `<`/`<=`, `>`/`>=`, `+`/`-`, `*`/`/`, `&&`/`||`, pattern `and`/`or`, delete `!`, pattern `not` and unary `-`, `0`/`1`. Attributes and string text are excluded; expressions inside interpolated strings remain executable. Constructors, operators and executable accessors follow crapper's members; local functions stay with their enclosing member. |
| Go | mutate4go: `+`/`-`, `*` to `/` only, comparisons, `==`/`!=`, `true`/`false`, `&&`/`||`, `0`/`1`. |
| TypeScript | the Java set, plus `===`/`!==`, `??` to `||`, and `?.` to `.`. A call or index drops `?.` (`a?.()` becomes `a()`). `.js`, `.jsx`, `.mjs`, and `.cjs` use these rules. |
| Rust | the Java set (`&&`/`||`, `!`, unary `-`). |
| Python | the Java set, spelled `and`/`or`, `not`, and `True`/`False`. |

## Tests and coverage

The baseline command must pass in the original project and in every unchanged
worker before any mutant runs. A worker baseline failure stops that file,
preserves its previous metrics, and does not count as a killed mutant. A mutant's
timeout is ten times the original baseline, and at least two seconds.
`--timeout-factor` changes the multiple. `--test-command` replaces the default.
Defaults:

| Language | Command | Where |
| --- | --- | --- |
| Clojure | `clj -M:spec --tag ~no-mutate` when Speclj is on the classpath, otherwise `clj -M:test` or `bb spec` | nearest `deps.edn` or `bb.edn` |
| Java | `mvn -q test -DexcludeTags=no-mutate` | nearest `pom.xml` |
| C# | baseline: `dotnet test`; mutant: `dotnet build`, then `dotnet test --no-build` | project root containing the solution or test project |
| Go | `go test -count=1` of the file's package | nearest `go.mod` |
| TypeScript | `npm test` | nearest `package.json` |
| Rust | `cargo test` | nearest `Cargo.toml` |
| Python | the project's `.venv` or `venv` Python running `pytest`, or `unittest discover` | nearest project file |

Coverage is generated with crapper's commands unless `--use-existing-coverage` or `--no-coverage` is set. A file missing from the report is treated as uncovered, and the run says so.

For C#, choose a `--root` containing the source projects, test projects, and
their required relative imports so the full build graph fits in each worker.
Copy workers live outside the original project, in the operating system's
temporary directory, so ancestor analyzer configuration is not inherited twice.
Build inputs such as `build/*.props` are copied; generated `bin`, `obj`, test
results and caches are omitted. Git metadata is not copied. A build that reads
Git version information needs that input supplied by its test command.
The default `dotnet test` runs in that directory and follows the project's
VSTest or Microsoft.Testing.Platform selection.
For each mutant, the default separates the build from the tests. A failed
build records `invalid` and skips the tests; only a successful build proceeds
to `dotnet test --no-build` with the test timeout.
Use `--test-command` to select a specific test project or filter. For example,
with .NET 10 and MTP in PowerShell:

```powershell
.\mutator.ps1 --root ..\MySolution --use-existing-coverage `
  --test-command 'dotnet test --project tests/MyTests/MyTests.csproj' `
  src/MyLibrary/Price.cs
```

Keep rebuilding enabled for mutation commands. C# reads covered lines from
crapper's Cobertura and LCOV reports. Existing Cobertura reports under
`target/coverage`, `coverage`, or `TestResults` can be reused with
`--use-existing-coverage`.

Coverage report paths use Crapper's shared source ownership rules. An ambiguous
basename does not cover every source file with that name. Each CLI run loads
reports and binds the project source index once; a subsequent run reads fresh
reports and source paths.

For a custom C# command, Mutator recognizes located compiler errors of the
form `file(line,column): error CS####` or `CSC : error CS####` as invalid.
Other nonzero results and test timeouts count as killed, preserving the
existing behavior for other languages. Custom command classification depends
on that output signal; use the default command for an explicit build phase.

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
