"""
Setup script for the TradingAgents package.
"""

from pathlib import Path

from setuptools import setup, find_namespace_packages

_HERE = Path(__file__).resolve().parent


def _runtime_requirements():
    """Direct runtime dependencies from the single maintained manifest.

    requirements.txt is the one source of truth; install_requires mirrors
    it so the two lists cannot drift. Build-only entries (setuptools) and
    pip directives (-r, index options) never become runtime requirements.
    """
    requirements = []
    for line in (_HERE / "requirements.txt").read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("-"):
            # pip directives (e.g. -r, --index-url) are invalid for
            # setuptools; refuse them rather than passing broken input.
            raise ValueError(
                "requirements.txt must not contain pip directives; "
                f"found: {entry!r}"
            )
        stripped = entry.split("#", 1)[0].strip()
        if not stripped or stripped.lower() == "setuptools":
            continue
        requirements.append(stripped)
    return requirements


setup(
    name="tradingagents",
    version="0.1.0",
    description="Auditable multi-agent trading research framework for paper trading, strategy testing, and risk-controlled execution",
    author="TradingAgents Team",
    author_email="yijia.xiao@cs.ucla.edu",
    url="https://github.com/ihsieh31/traders",
    project_urls={
        "Source": "https://github.com/ihsieh31/traders",
        "Upstream TradingAgents": "https://github.com/TauricResearch/TradingAgents",
        "Upstream AlpacaTradingAgent": "https://github.com/huygiatrng/AlpacaTradingAgent",
    },
    packages=find_namespace_packages(include=["tradingagents*", "cli*", "webui*"]),
    include_package_data=True,
    package_data={
        "tradingagents.prompts": [
            "templates/*.md",
            "templates/*/*.md",
        ]
    },
    install_requires=_runtime_requirements(),
    python_requires=">=3.10",
    entry_points={
        "console_scripts": [
            "tradingagents=cli.main:app",
            "tradingagents-web=webui.cli:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Financial and Trading Industry",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Topic :: Office/Business :: Financial :: Investment",
    ],
)
