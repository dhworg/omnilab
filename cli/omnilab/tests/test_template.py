"""Tests for omnilab.template — discovery, render, install."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnilab.template import (
    LocalRegistry,
    TemplateInfo,
    TemplateNotFound,
    find_repo_templates_dir,
    install_template,
    render,
)

# ---- render -------------------------------------------------------------


def test_render_basic_substitution():
    assert render("hello {{name}}", {"name": "world"}) == "hello world"


def test_render_with_whitespace_in_braces():
    assert render("{{ name }}", {"name": "x"}) == "x"


def test_render_repeated_var():
    assert render("a {{x}} b {{x}}", {"x": "Q"}) == "a Q b Q"


def test_render_unknown_variable_raises():
    with pytest.raises(KeyError):
        render("{{missing}}", {})


def test_render_no_substitution_when_no_vars():
    assert render("plain text", {}) == "plain text"


# ---- TemplateInfo --------------------------------------------------------


def test_template_info_from_yaml():
    text = "name: foo\ndescription: bar\nversion: '2'\nvariables: [a, b]\nfiles: [x.txt]\n"
    info = TemplateInfo.from_yaml(text)
    assert info.name == "foo"
    assert info.description == "bar"
    assert info.version == "2"
    assert info.variables == ["a", "b"]
    assert info.files == ["x.txt"]


def test_template_info_minimal():
    info = TemplateInfo.from_yaml("name: foo\n")
    assert info.name == "foo"
    assert info.description == ""
    assert info.version == "1"
    assert info.variables == []
    assert info.files == []


# ---- LocalRegistry -------------------------------------------------------


def _make_template(root: Path, name: str, files: dict[str, str]) -> None:
    """Helper: build a complete template tree for tests."""
    tdir = root / name
    (tdir / "files").mkdir(parents=True, exist_ok=True)
    (tdir / "template.yaml").write_text(
        f"name: {name}\ndescription: t-{name}\nfiles: {list(files.keys())}\n"
    )
    for rel, content in files.items():
        path = tdir / "files" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_local_registry_lists_templates(tmp_path: Path):
    _make_template(tmp_path, "alpha", {"omnilab.yaml": "x"})
    _make_template(tmp_path, "beta", {"omnilab.yaml": "y"})
    reg = LocalRegistry(tmp_path)
    assert reg.list_names() == ["alpha", "beta"]


def test_local_registry_skips_dirs_without_template_yaml(tmp_path: Path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "files").mkdir()
    # No template.yaml — should be skipped.
    assert LocalRegistry(tmp_path).list_names() == []


def test_local_registry_fetch_missing_raises(tmp_path: Path):
    reg = LocalRegistry(tmp_path)
    with pytest.raises(TemplateNotFound):
        reg.fetch("nope")


# ---- install_template ---------------------------------------------------


def test_install_template_renders_and_writes(tmp_path: Path):
    _make_template(
        tmp_path,
        "t1",
        {"omnilab.yaml": "name: {{project_name}}\n"},
    )
    info = TemplateInfo(name="t1", description="", files=["omnilab.yaml"])
    target = tmp_path / "out"
    written = install_template(
        info=info,
        template_root=tmp_path / "t1",
        target=target,
        variables={"project_name": "myproj"},
    )
    assert len(written) == 1
    text = (target / "omnilab.yaml").read_text()
    assert text == "name: myproj\n"


def test_install_template_refuses_overwrite(tmp_path: Path):
    _make_template(
        tmp_path,
        "t1",
        {"omnilab.yaml": "name: {{project_name}}\n"},
    )
    info = TemplateInfo(name="t1", description="", files=["omnilab.yaml"])
    target = tmp_path / "out"
    target.mkdir()
    (target / "omnilab.yaml").write_text("existing")
    with pytest.raises(FileExistsError):
        install_template(
            info=info,
            template_root=tmp_path / "t1",
            target=target,
            variables={"project_name": "x"},
        )


def test_install_template_handles_subdirs(tmp_path: Path):
    _make_template(
        tmp_path,
        "t1",
        {"sub/main.cpp": "// hello {{project_name}}\n"},
    )
    info = TemplateInfo(name="t1", description="", files=["sub/main.cpp"])
    target = tmp_path / "out"
    written = install_template(
        info=info,
        template_root=tmp_path / "t1",
        target=target,
        variables={"project_name": "z"},
    )
    assert len(written) == 1
    assert (target / "sub" / "main.cpp").read_text() == "// hello z\n"


def test_install_template_respects_declared_files_allowlist(tmp_path: Path):
    """If template.yaml declares `files`, files outside that list are skipped."""
    _make_template(
        tmp_path,
        "t1",
        {"keep.yaml": "k", "skip.yaml": "s"},
    )
    info = TemplateInfo(name="t1", description="", files=["keep.yaml"])
    target = tmp_path / "out"
    install_template(
        info=info,
        template_root=tmp_path / "t1",
        target=target,
        variables={},
    )
    assert (target / "keep.yaml").exists()
    assert not (target / "skip.yaml").exists()


def test_install_template_unknown_var_propagates(tmp_path: Path):
    _make_template(tmp_path, "t1", {"omnilab.yaml": "hi {{nope}}\n"})
    info = TemplateInfo(name="t1", description="", files=["omnilab.yaml"])
    with pytest.raises(KeyError):
        install_template(
            info=info,
            template_root=tmp_path / "t1",
            target=tmp_path / "out",
            variables={"project_name": "x"},
        )


# ---- repo template dir resolver -----------------------------------------


def test_find_repo_templates_dir_walks_up(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "templates").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("repo")
    sub = repo / "deep" / "deeper"
    sub.mkdir(parents=True)
    found = find_repo_templates_dir(sub)
    assert found == repo / "templates"


def test_find_repo_templates_dir_returns_none_outside_repo(tmp_path: Path):
    assert find_repo_templates_dir(tmp_path) is None


# ---- end-to-end: foundational templates exist + render ------------------


def test_foundational_templates_present():
    """The three foundational templates from spec § "Templates" must
    exist under templates/ in the repo and parse correctly."""
    repo = find_repo_templates_dir()
    assert repo is not None, "test must run inside the OmniLab repo"
    reg = LocalRegistry(repo)
    names = set(reg.list_names())
    assert {"nav2-base", "micro-ros-blink", "quadruped-walker"}.issubset(names)


def test_foundational_templates_install_clean(tmp_path: Path):
    """End-to-end: each foundational template renders without errors and
    produces a parseable omnilab.yaml."""
    import yaml

    from omnilab.manifest import OmnilabManifest

    repo = find_repo_templates_dir()
    assert repo is not None
    reg = LocalRegistry(repo)

    for name in ("nav2-base", "micro-ros-blink", "quadruped-walker"):
        path = reg.fetch(name)
        info = TemplateInfo.from_yaml((path / "template.yaml").read_text())
        target = tmp_path / name
        install_template(
            info=info,
            template_root=path,
            target=target,
            variables={"project_name": f"e2e-{name}"},
        )
        manifest = OmnilabManifest.model_validate(
            yaml.safe_load((target / "omnilab.yaml").read_text())
        )
        assert manifest.name == f"e2e-{name}"
