"""
Setup script for the TradingAgents package.
"""

from setuptools import setup, find_namespace_packages

setup(
    name="tradingagents",
    version="0.1.0",
    description="Auditable multi-agent trading research framework for paper trading, strategy testing, and risk-controlled execution",
    author="TradingAgents Team",
    author_email="yijia.xiao@cs.ucla.edu",
    url="https://github.com/TauricResearch",
    packages=find_namespace_packages(include=["tradingagents*", "cli*", "webui*"]),
    include_package_data=True,
    package_data={
        "tradingagents.prompts": [
            "templates/*.md",
            "templates/*/*.md",
        ]
    },
    install_requires=[
        "openai>=2.33.0,<3.0.0",
        "langchain>=0.3.27,<0.4.0",
        "langchain-core>=0.3.84,<1.0.0",
        "langchain-openai>=0.3.35,<0.4.0",
        "langchain-anthropic>=0.3.22,<0.4.0",
        "langchain-google-genai>=2.1.12,<3.0.0",
        "langchain-experimental>=0.3.4,<0.4.0",
        "langgraph>=0.6.6,<0.7.0",
        "numpy>=1.24.0",
        "pandas>=2.0.0",
        "praw>=7.7.0",
        "stockstats>=0.5.4",
        "typer>=0.9.0",
        "rich>=13.0.0",
        "questionary>=2.0.1",
        "gradio>=4.0.0",
        "plotly>=5.18.0",
    ],
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
