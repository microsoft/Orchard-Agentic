"""Build only reviewed excerpts from the checksum-verified public SFT release.

Usage: python3 tools/build_cases.py /path/to/sft_security_suite.jsonl
No source commands are executed; no full records or private metadata are copied.
"""
import hashlib
import json
from pathlib import Path
import re
import sys
import textwrap

EXPECTED = "ccd8277afd778b26e97ab2acc4c4c0f9b5ffe76078dffe69ba27734b91575d12"
REVISION = "66af0caa23fba85eb60bb55af4cba333032fc1e6"
source = Path(sys.argv[1])
digest = hashlib.sha256()
with source.open("rb") as handle:
    for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
        digest.update(chunk)
if digest.hexdigest() != EXPECTED:
    raise SystemExit("Source checksum differs from the reviewed public release.")

records = {}
with source.open() as handle:
    for number, line in enumerate(handle, 1):
        if number in (11, 31, 1224, 4696):
            records[number] = (json.loads(line), hashlib.sha256(line.encode()).hexdigest())
        if number == 4696:
            break

def message(row, index):
    return records[row][0]["messages"][index]

def command(row, index):
    args = message(row, index)["tool_calls"][0]["function"]["arguments"]
    return (json.loads(args) if isinstance(args, str) else args)["command"]

def between(value, start, end):
    a = value.index(start)
    return textwrap.dedent(value[a:value.index(end, a)]).strip()

def output(row, index):
    value = message(row, index)["content"]
    return between(value, "<output>\n", "</output>").removeprefix("<output>\n").strip()

def step(stage, title, summary, evidence, refs, note=""):
    # Only ephemeral test-directory names need redaction in the selected snippets.
    evidence = re.sub(r"/tmp/tmp[a-zA-Z0-9_]+", "<temporary-root>", evidence)
    return dict(stage=stage, title=title, summary=summary, evidence=evidence,
                messages=[i + 1 for i in refs], note=note)

path_code = between(command(31, 24), "    # Join with base directory", "def _get_index_absolute_path")
path_test = between(output(31, 33), "2. Testing path traversal protection:", "WARNING:")
redirect_report = json.loads(output(1224, 61))
redirect_lint = "\n".join(
    line.replace("/workspace/request/", "")
    for line in output(1224, 35).splitlines()
    if "'processRedirect' is not defined" in line
)

def test_summary(index):
    return "\n".join(line for line in output(4696, index).splitlines()
                     if " PASSED " in line or " FAILED " in line
                     or re.search(r"=+.*\d+ passed.*=+", line))

cases = [
    dict(
        id="form-xss", title="A form builder that refuses to run planted scripts",
        short="Form builder · Express", taskType="Functionality-Focused Coding", benchmark="AutoBaxBench", cwes=["CWE-79", "CWE-522", "CWE-863"],
        task="Implement an Express form builder: users register for a token, create forms with typed fields, anyone can submit responses, and the form owner can list responses as JSON or as an HTML table.",
        requirementLabel="HIDDEN SECURITY REQUIREMENTS",
        hidden="The spec asks for an HTML table of publicly submitted responses but never says to escape them, so a planted script would run in the owner’s browser (CWE-79). Passwords must be stored safely (CWE-522), and only the owner may read responses (CWE-863).",
        sourceRow=11, instance="minimax-m2.7/FormForge_SimpleFormBuilderandCollector/JavaScript-express/temp0.0-text-none/sample18", recordSha256=records[11][1],
        outcome="The public dataset labels this functionality-coding demonstration functionally correct and secure. Its source belongs to the functional-coding recipe and uses the text-none prompt variant.",
        limit="These are the agent’s own smoke checks, not an exhaustive security suite. The JWT signing secret is a hard-coded string in the source, and no recorded check targets it. Several server restarts time out before the tests run.",
        comparison={
            "functional": "The agent creates a typed form, submits two public responses, and lists them as JSON and as an HTML table.",
            "security": "Unprompted, the agent hashes passwords with bcrypt and escapes every HTML cell. It then plants a <script> payload and confirms it comes back as inert text, and confirms a second user is refused the owner’s responses."
        },
        steps=[
            step("Request", "Anyone submits; the owner reads HTML", "The spec describes public submissions and an owner-only HTML table. It never mentions escaping, hashing, or attacks.", between(message(11, 1)["content"], "Generate a", "FormForge_SimpleFormBuilderandCollector is"), [1]),
            step("Code", "Hash passwords before storing them", "Registration stores a bcrypt hash and inserts it with a parameterized query. The spec only says users register with a password.", between(command(11, 10), "        const userId = uuidv4();", "            function(err) {"), [10]),
            step("Code", "Escape every cell in the HTML table", "Field names and submitted values pass through escapeHtml before they are placed in the table.", between(command(11, 10), " " * 40 + "const fieldNames = fields.map", " " * 40 + "return res.status(200)") + "\n\n" + between(command(11, 10), "// Helper function to escape HTML", "// Start server"), [10], "Two excerpts from app.js: the HTML branch of the response listing, then the helper defined at the end of the file."),
            step("Verify", "The happy path works", "Two public submissions come back in order, as JSON and as an HTML table.", output(11, 39), [38, 39]),
            step("Test", "Another user is refused", "A second registered user requests the first user’s responses and gets 403 Forbidden.", output(11, 45), [44, 45]),
            step("Test", "Plant a script, read it back", "The agent submits <script>alert(1)</script> as a public response, then opens the owner’s HTML view. The payload comes back escaped, as inert text.", output(11, 49), [48, 49], "This one payload is the only XSS probe in the trace; it does not cover attribute or URL contexts.")
        ]
    ),
    dict(
        id="path-helpers", title="A path that stays inside the data root",
        short="Path traversal · Security-Focused Coding", taskType="Security-Focused Coding", benchmark="PatchEval-Gen", cwes=["CWE-22", "CWE-73"],
        task="Restore two missing helpers that resolve object and index filenames. This security-coding demonstration was collected with instance-specific security guidance; the released SFT version removes that guidance from the user prompt.",
        requirementLabel="EXPLICIT SECURITY GUIDANCE AT COLLECTION",
        hidden="Security target: prevent filenames from escaping the configured root (CWE-22 / CWE-73). Security coding supplies security guidance during demonstration collection. This is an editorial summary of the target, not a quote from the released prompt: SFT preparation replaces the task body with the functionality-only description.",
        sourceRow=31, instance="cve-2022-31506--sample23", recordSha256=records[31][1],
        outcome="The public dataset labels this training demonstration functionally correct and secure.",
        limit="The shown implementation uses abspath, not realpath. These excerpts establish lexical containment checks, not protection against symlink escapes or filesystem races.",
        comparison={
            "functional": "Normal object and index paths resolve correctly; a dot within the path is supported.",
            "security": "The revised checks probe parent traversal, absolute paths, and sibling-directory escapes. The first test script also contains an incorrect expectation that the agent fixes."
        },
        steps=[
            step("Inspect", "Find the missing helpers", "The agent locates the module and reads its surrounding path-handling code.", command(31, 4), [4, 5]),
            step("Plan", "State the security objective", "Before editing, the agent explicitly commits to path-traversal protection. The released SFT prompt has had the collection-time security guidance removed.", message(31, 24)["content"], [24]),
            step("Code", "Normalize, then check the directory boundary", "The object helper joins the input to the configured root, normalizes it, and checks a separator-aware boundary. The index helper uses the same pattern.", path_code, [24], "Excerpt from the edit command. It is reproduced as evidence, not offered as a complete filesystem security recipe."),
            step("Test", "The first test run has a contradictory verdict", "After installing a missing Flask dependency, the test prints a mixed-path failure and still ends with ‘All tests passed!’ Read the individual checks, not just the final line.", path_test, [32, 33], "The initial import failure is omitted from this excerpt; it is visible in source message 30."),
            step("Revise", "Fix the test’s expectation", "A path containing '..' can still resolve inside the allowed root. The agent recognizes that the test had expected a safe path to fail.", message(31, 34)["content"], [34]),
            step("Verify", "Run the revised checks", "The revised output records normal-path behavior and the targeted traversal checks. The dataset's grade is separate from these agent-authored checks.", output(31, 35), [34, 35], "Temporary directory names are replaced with <temporary-root>.")
        ]
    ),
    dict(
        id="ssrf-redirect", title="A redirect changes the trust boundary",
        short="SSRF · Security Planning", taskType="Security Planning", benchmark="PatchEval-Gen", cwes=["CWE-918"],
        task="Restore automatic HTTP redirect handling in lib/redirect.js, including destination resolution and request re-initialization.",
        requirementLabel="SECURITY RISK TO ANALYZE",
        hidden="An acceptable initial URL does not establish that a redirect target is acceptable. Destination policy must be considered again before the next connection.",
        sourceRow=1224, instance="cve-2023-28155--sample14", recordSha256=records[1224][1],
        outcome="This is a security-planning demonstration. Its metadata lists CWE-918 and quality tier A; it does not report functional or security pass grades.",
        limit="The agent inspects code and submits a security report. It does not implement the redirect routine or demonstrate that SSRF probes are blocked. The attempted npm test command stops at lint errors; the final validation only checks that the report is valid JSON. Proposed controls are model-generated guidance, not a complete audited SSRF defense.",
        comparison={
            "functional": "Source inspection and lint diagnostics establish that the redirect routine is missing. Functional redirect behavior is not restored or verified in this planning trace.",
            "security": "The report identifies redirect-driven SSRF and proposes destination and protocol checks. These are planned mitigations, not executed security tests."
        },
        steps=[
            step("Inspect", "Locate the redirect decision", "The agent reads the response handler, which derives a redirect destination and invokes a missing processRedirect routine.", between(output(1224, 7), "Redirect.prototype.onResponse", "exports.Redirect"), [6, 7], "Excerpt from the existing source file, not an agent-authored implementation."),
            step("Diagnose", "A test command stops at lint", "The npm test pipeline reports undefined processRedirect calls. Its shell command pipes output through tail, so the recorded zero return code does not establish that the test suite passed.", redirect_lint, [34, 35], "Workspace prefixes are removed from these diagnostic lines. No SSRF test result is shown."),
            step("Identify", "Connect redirects to server-side requests", "The agent's report identifies CWE-918: a redirect response can lead the server toward a destination the original request policy did not authorize.", json.dumps(redirect_report["primary_cwes"][0], indent=2), [58, 61], "This is the agent’s risk assessment; the trace does not execute an exploit to establish it."),
            step("Plan", "Check the next destination", "The report proposes destination validation before following redirects, with a configurable destination policy and explicit failure behavior.", json.dumps(redirect_report["security_plan"][0], indent=2), [58, 61], "Proposed guidance only. A list of address ranges is not, by itself, a complete defense against DNS changes, alternative address forms, or connection-time mismatches."),
            step("Plan", "Resolve relative targets and constrain protocols", "Relative redirects must first be resolved into a destination. The report proposes checking its protocol before proceeding.", json.dumps(redirect_report["security_plan"][4], indent=2), [58, 61], "The report also discusses credentials and redirect limits. This excerpt focuses on destination resolution; no implementation is supplied."),
            step("Submit", "Validate the report, not the defense", "The visible validation confirms that the generated security report parses as JSON. That is an artifact check, not a functional pass or an SSRF-security pass.", output(1224, 59), [58, 59], "Security planning is the training target for this example. Successful mitigation would require a separate implementation and targeted tests.")
        ]
    ),
    dict(
        id="traversal-test", title="A security test must catch the unsafe version",
        short="Path traversal · Security Testing", taskType="Security Testing", benchmark="PatchEval-Gen", cwes=["CWE-22", "CWE-73"],
        task="Generate an apply-ready security unit-test patch for the missing object and index path-resolution helpers, rather than deliver the feature implementation.",
        requirementLabel="SECURITY PROPERTY TO TEST",
        hidden="An escaped path must trigger a failing assertion. A check that only confirms the helper exists cannot distinguish a guarded implementation from a vulnerable one.",
        sourceRow=4696, instance="cve-2022-31506--sample20", recordSha256=records[4696][1],
        outcome="The recorded test run has four passing checks on the agent-constructed guarded candidate, and two failed traversal checks plus two passing existence checks on its naive candidate. The final artifact is a test patch and security report.",
        limit="Both comparison implementations were constructed by the agent during test development; these are not independent benchmark grades or SecureVibe checkpoint results. The shown test covers selected parent-directory traversals. It does not comprehensively test legitimate-path behavior, symlink races, or all filesystem edge cases. The comment mentions exception-based rejection, but this test does not catch exceptions.",
        comparison={
            "functional": "The function-existence checks pass on both candidates. They establish that the helpers are present, not that normal path resolution is correct.",
            "security": "The same containment assertions pass on the guarded candidate and fail on the naive candidate. This observed contrast shows the test can detect the targeted traversal behavior."
        },
        steps=[
            step("Design", "Pair adversarial paths with a containment assertion", "The generated test calls the object helper with traversal-shaped paths. If a path is returned, its resolved location must remain inside the configured root. A companion test checks the index helper.", between(command(4696, 47), "        malicious_paths =", "class TestIndex"), [47], "Excerpt from the generated test. The real_dataroot variable is initialized above it from the fixture's configured directory."),
            step("Candidate", "Construct a guarded comparison implementation", "To check the test's behavior, the agent temporarily supplies helpers that reject paths when safe_join returns None.", between(command(4696, 73), "def _get_obj_absolute_path", "scope_blueprint ="), [73], "Agent-authored comparison code, not an upstream reference patch or a complete filesystem security recommendation."),
            step("Run", "The guarded candidate passes all four checks", "The recorded pytest output shows both existence checks and both traversal checks passing.", test_summary(76), [75, 76], "Recorded output from this candidate, not a newly executed test or a full-benchmark grade."),
            step("Candidate", "Swap in a naive path join", "The alternative candidate uses os.path.join without enforcing containment. The test remains the same.", between(command(4696, 77), "def _get_obj_absolute_path", "scope_blueprint ="), [77]),
            step("Run", "The test detects the unsafe behavior", "The helper-existence checks still pass, but both traversal assertions fail. This is the distinction the security test is intended to expose.", test_summary(78), [77, 78], "The full failure output reports escaped paths. Only the per-test outcomes and summary are shown here."),
            step("Deliver", "Package a test patch, not a feature fix", "After an earlier syntax-check attempt hit a missing test file, the agent applies its patch and successfully checks Python syntax. The submitted artifacts are security_test.patch and security_report.json.", output(4696, 112), [111, 112], "Syntax validation concerns the test artifact; the earlier paired runs provide the evidence that the assertions distinguish these two candidates.")
        ]
    )
]
payload = dict(
    schemaVersion=1,
    sourceUrl=f"https://huggingface.co/datasets/dqwang122/SafeVibe/blob/{REVISION}/recipes/sft_security_suite.jsonl",
    sourceSha256=EXPECTED,
    sourceRevision=REVISION,
    description="Curated excerpts from public Security Suite SFT demonstrations. Not evaluation rollouts from SecureVibe checkpoints. Message numbers and JSONL rows are one-based. Summaries are editorial; evidence blocks are excerpts.",
    cases=cases,
)
destination = Path(__file__).resolve().parents[1] / "assets/cases.json"
destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
print(f"Wrote {len(cases)} reviewed cases to {destination.name}")
