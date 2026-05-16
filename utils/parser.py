"""output parsers for structured llm responses.

agents produce output in a documented text format using section headers
(SECTION_NAME: on its own line). these parsers extract that structure into
python dicts so the pipeline can act on it without stringly-typed string searches.

each section parser is deliberately lenient — llms don't always follow instructions
perfectly, so the parsers try multiple patterns before giving up.
"""

import re
import json


# ── architect output ──────────────────────────────────────────────────────────

# sections the architect agent is expected to produce
ARCHITECT_SECTIONS = [
    "TECH_STACK",
    "REQUIREMENTS_TXT",
    "PACKAGE_JSON",
    "PUBSPEC_YAML",
    "DIRECTORY_STRUCTURE",
    "ARCHITECTURE_MD",
    "CODING_STANDARDS_MD",
    "MEMORY_ENTRY",
]


def parse_sections(raw: str, section_names: list[str]) -> dict[str, str]:
    """split structured llm output into a dict keyed by section name.

    expects sections delimited by 'SECTION_NAME:' on its own line.
    returns {section_name: content_string} with whitespace stripped.
    """
    # build a regex that matches any of the given section header names
    pattern = r"^(" + "|".join(section_names) + r"):\s*$"
    # split on headers — result is [before_first_header, header, content, header, content, ...]
    parts = re.split(pattern, raw, flags=re.MULTILINE)

    sections: dict[str, str] = {}
    i = 1
    while i < len(parts) - 1:
        header = parts[i].strip()
        content = parts[i + 1].strip()
        sections[header] = content
        i += 2

    return sections


def parse_architect_output(raw: str) -> dict[str, str]:
    """parse the architect agent's output into a dict keyed by section name."""
    return parse_sections(raw, ARCHITECT_SECTIONS)


def parse_directory_list(dir_section: str) -> list[str]:
    """parse DIRECTORY_STRUCTURE into a list of directory paths.

    strips tree-drawing characters (│ ├ └ ─) if the llm used them.
    skips lines that look like files or inline descriptions.
    each returned path ends with '/'.
    """
    dirs: list[str] = []
    for line in dir_section.strip().splitlines():
        # strip ascii/unicode tree-drawing characters
        cleaned = re.sub(r"^[\s│├└─┬┤┐┘┌┼|+\-`]+", "", line).strip()
        if not cleaned:
            continue
        # skip lines that are descriptions (have spaces and don't end with /)
        if " " in cleaned and not cleaned.endswith("/"):
            continue
        # skip lines that look like files (have a dotted extension)
        if re.search(r"\.\w+$", cleaned) and not cleaned.endswith("/"):
            continue
        if not cleaned.endswith("/"):
            cleaned += "/"
        dirs.append(cleaned)
    return dirs


# ── sprint plan parsing (project lead output) ─────────────────────────────────

def parse_sprint_plan(raw: str) -> dict | None:
    """extract the json sprint plan from project lead output.

    looks for SPRINT_PLAN: or UPDATED_SPRINT_PLAN: followed by a json code block.
    falls back to raw json extraction with brace balancing if the code block is missing.
    returns parsed dict or None if nothing parseable is found.
    """
    # primary: json inside a code block
    pattern = r"(?:SPRINT_PLAN|UPDATED_SPRINT_PLAN):\s*```(?:json)?\s*(\{.*?\})\s*```"
    match = re.search(pattern, raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # fallback: raw json after the header, no code block — find end by brace balancing
    pattern2 = r"(?:SPRINT_PLAN|UPDATED_SPRINT_PLAN):\s*(\{.*)"
    match2 = re.search(pattern2, raw, re.DOTALL)
    if match2:
        json_text = match2.group(1).strip()
        depth = 0
        end = 0
        for i, ch in enumerate(json_text):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > 0:
            try:
                return json.loads(json_text[:end])
            except json.JSONDecodeError:
                pass

    return None


def parse_review_summary(raw: str) -> str:
    """extract the REVIEW_SUMMARY section from project lead review output."""
    match = re.search(r"^REVIEW_SUMMARY:\s*\n(.*?)(?=\n(?:UPDATED_SPRINT_PLAN|MEMORY_ENTRY):|\Z)",
                      raw, re.MULTILINE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


# ── developer agent output parsing ────────────────────────────────────────────

def parse_dev_files(raw: str) -> tuple[dict[str, str], dict[str, str]]:
    """parse developer agent output into files to create and modify.

    agents write files inside FILES_TO_CREATE: and FILES_TO_MODIFY: sections,
    each file delimited by --- FILE: path --- / --- END FILE --- markers.

    returns (files_to_create, files_to_modify) — both are {filepath: contents}.
    """
    files_to_create: dict[str, str] = {}
    files_to_modify: dict[str, str] = {}

    create_match = re.search(
        r"^FILES_TO_CREATE:\s*\n(.*?)(?=\n^FILES_TO_MODIFY:|\n^MEMORY_ENTRY:|\Z)",
        raw, re.MULTILINE | re.DOTALL
    )
    if create_match:
        block = create_match.group(1).strip()
        if block.lower() != "none":
            files_to_create = _extract_file_blocks(block)

    modify_match = re.search(
        r"^FILES_TO_MODIFY:\s*\n(.*?)(?=\n^MEMORY_ENTRY:|\Z)",
        raw, re.MULTILINE | re.DOTALL
    )
    if modify_match:
        block = modify_match.group(1).strip()
        if block.lower() != "none":
            files_to_modify = _extract_file_blocks(block)

    # fallback: if neither section header was found, try to extract any file blocks
    # from the raw output — agent may have forgotten the section wrapper
    if not files_to_create and not files_to_modify:
        all_files = _extract_file_blocks(raw)
        if all_files:
            files_to_create = all_files

    return files_to_create, files_to_modify


def parse_qa_output(raw: str) -> dict:
    """parse qa agent output into structured results.

    returns dict with keys: test_files, test_results, status, errors.
    """
    result: dict = {
        "test_files": {},
        "test_results": "",
        "status": "UNKNOWN",
        "errors": "",
    }

    tf_match = re.search(
        r"^TEST_FILES_CREATED:\s*\n(.*?)(?=\n^TEST_RESULTS:|\n^MEMORY_ENTRY:|\Z)",
        raw, re.MULTILINE | re.DOTALL
    )
    if tf_match:
        block = tf_match.group(1).strip()
        if block.lower() != "none":
            result["test_files"] = _extract_file_blocks(block)

    tr_match = re.search(
        r"^TEST_RESULTS:\s*\n(.*?)(?=\n^STATUS:|\n^MEMORY_ENTRY:|\Z)",
        raw, re.MULTILINE | re.DOTALL
    )
    if tr_match:
        result["test_results"] = tr_match.group(1).strip()

    st_match = re.search(r"^STATUS:\s*\n?(.+?)$", raw, re.MULTILINE)
    if st_match:
        result["status"] = st_match.group(1).strip()

    er_match = re.search(
        r"^ERRORS:\s*\n(.*?)(?=\n^MEMORY_ENTRY:|\Z)",
        raw, re.MULTILINE | re.DOTALL
    )
    if er_match:
        result["errors"] = er_match.group(1).strip()

    return result


def _extract_file_blocks(text: str) -> dict[str, str]:
    """extract file blocks from agent output.

    primary format:
        --- FILE: path/to/file.py ---
        <contents>
        --- END FILE ---

    fallback format (when agents ignore the delimiter instructions):
        ```python
        # File: path/to/file.py
        <contents>
        ```

    returns {filepath: contents}. strips markdown code fences if present inside the block.
    """
    files: dict[str, str] = {}

    # primary: --- FILE: ... --- delimiters
    pattern = r"---\s*FILE:\s*(.+?)\s*---\s*\n(.*?)---\s*END(?:\s*FILE)?\s*---"
    for match in re.finditer(pattern, text, re.DOTALL):
        filepath = match.group(1).strip()
        contents = match.group(2)
        # strip any inner markdown code fence the llm may have added
        contents = re.sub(r"^```\w*\s*\n", "", contents)
        contents = re.sub(r"\n```\s*$", "", contents)
        contents = contents.strip("\n")
        files[filepath] = contents

    if files:
        return files

    # fallback: ```lang\n# File: path ... ``` blocks
    fallback_pattern = r"```(?:\w+)?\s*\n#?\s*(?:File:\s*)?([^\n`]+\.(?:py|html|css|js|json|txt|md))\s*\n(.*?)```"
    for match in re.finditer(fallback_pattern, text, re.DOTALL):
        filepath = match.group(1).strip()
        contents = match.group(2).strip("\n")
        files[filepath] = contents

    return files
