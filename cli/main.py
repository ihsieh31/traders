from typing import Optional
import datetime
import typer
from rich.console import Console
from rich.panel import Panel
from rich.spinner import Spinner
from rich.live import Live
from rich.columns import Columns
from rich.markdown import Markdown
from rich.layout import Layout
from rich.text import Text
from rich.live import Live
from rich.table import Table
from collections import deque
import time
from rich.tree import Tree
from rich import box
from rich.align import Align
from rich.rule import Rule

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.retry import ProviderFailure
from tradingagents.graph.checkpointer import clear_checkpoint
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.run_logger import get_run_audit_logger
from tradingagents.agents.schemas import trade_intent_action
from cli.models import AnalystType
from cli.utils import *

console = Console()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Auditable Multi-Agent Trading Research Framework",
    add_completion=True,  # Enable shell completion
)


# Create a deque to store recent messages with a maximum length
class MessageBuffer:
    def __init__(self, max_length=100):
        self.messages = deque(maxlen=max_length)
        self.tool_calls = deque(maxlen=max_length)
        self.current_report = None
        self.final_report = None  # Store the complete final report
        self.agent_status = {
            # Analyst Team
            "Market Analyst": "pending",
            "Social Analyst": "pending",
            "News Analyst": "pending",
            "Fundamentals Analyst": "pending",
            "Macro Analyst": "pending",
            # Research Team
            "Bull Researcher": "pending",
            "Bear Researcher": "pending",
            "Research Manager": "pending",
            # Trading Team
            "Trader": "pending",
            # Risk Management Team
            "Risky Analyst": "pending",
            "Neutral Analyst": "pending",
            "Safe Analyst": "pending",
            # Portfolio Management Team
            "Portfolio Manager": "pending",
        }
        self.current_agent = None
        self.report_sections = {
            "market_report": None,
            "sentiment_report": None,
            "news_report": None,
            "fundamentals_report": None,
            "macro_report": None,
            "investment_plan": None,
            "trader_investment_plan": None,
            "final_trade_decision": None,
        }

    def add_message(self, message_type, content):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.messages.append((timestamp, message_type, content))

    def add_tool_call(self, tool_name, args):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.tool_calls.append((timestamp, tool_name, args))

    def update_agent_status(self, agent, status):
        if agent in self.agent_status:
            self.agent_status[agent] = status
            self.current_agent = agent

    def update_report_section(self, section_name, content):
        if section_name in self.report_sections:
            self.report_sections[section_name] = content
            self._update_current_report()

    def _update_current_report(self):
        # For the panel display, only show the most recently updated section
        latest_section = None
        latest_content = None

        # Find the most recently updated section
        for section, content in self.report_sections.items():
            if content is not None:
                latest_section = section
                latest_content = content

        if latest_section and latest_content:
            # Format the current section for display
            section_titles = {
                "market_report": "Market Analysis",
                "sentiment_report": "Social Sentiment",
                "news_report": "News Analysis",
                "fundamentals_report": "Fundamentals Analysis",
                "macro_report": "Macro Analysis",
                "investment_plan": "Research Team Decision",
                "trader_investment_plan": "Trading Team Plan",
                "final_trade_decision": "Portfolio Management Decision",
            }
            self.current_report = (
                f"### {section_titles[latest_section]}\n{latest_content}"
            )

        # Update the final complete report
        self._update_final_report()

    def _update_final_report(self):
        report_parts = []

        # Analyst Team Reports
        if any(
            self.report_sections[section]
            for section in [
                "market_report",
                "sentiment_report",
                "news_report",
                "fundamentals_report",
                "macro_report",
            ]
        ):
            report_parts.append("## Analyst Team Reports")
            if self.report_sections["market_report"]:
                report_parts.append(
                    f"### Market Analysis\n{self.report_sections['market_report']}"
                )
            if self.report_sections["sentiment_report"]:
                report_parts.append(
                    f"### Social Sentiment\n{self.report_sections['sentiment_report']}"
                )
            if self.report_sections["news_report"]:
                report_parts.append(
                    f"### News Analysis\n{self.report_sections['news_report']}"
                )
            if self.report_sections["fundamentals_report"]:
                report_parts.append(
                    f"### Fundamentals Analysis\n{self.report_sections['fundamentals_report']}"
                )
            if self.report_sections["macro_report"]:
                report_parts.append(
                    f"### Macro Analysis\n{self.report_sections['macro_report']}"
                )

        # Research Team Reports
        if self.report_sections["investment_plan"]:
            report_parts.append("## Research Team Decision")
            report_parts.append(f"{self.report_sections['investment_plan']}")

        # Trading Team Reports
        if self.report_sections["trader_investment_plan"]:
            report_parts.append("## Trading Team Plan")
            report_parts.append(f"{self.report_sections['trader_investment_plan']}")

        # Portfolio Management Decision
        if self.report_sections["final_trade_decision"]:
            report_parts.append("## Portfolio Management Decision")
            report_parts.append(f"{self.report_sections['final_trade_decision']}")

        self.final_report = "\n\n".join(report_parts) if report_parts else None


message_buffer = MessageBuffer()


def create_layout():
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )
    layout["main"].split_column(
        Layout(name="upper", ratio=3), Layout(name="analysis", ratio=5)
    )
    layout["upper"].split_row(
        Layout(name="progress", ratio=2), Layout(name="messages", ratio=3)
    )
    return layout


def update_display(layout, spinner_text=None):
    # Header with welcome message
    layout["header"].update(
        Panel(
            "[bold green]Welcome to AlpacaTradingAgent CLI[/bold green]\n"
            "[dim]Auditable multi-agent trading research framework[/dim]\n"
            "[dim]© [Tauric Research](https://github.com/TauricResearch)[/dim]",
            title="Welcome to AlpacaTradingAgent",
            border_style="green",
            padding=(1, 2),
            expand=True,
        )
    )

    # Progress panel showing agent status
    progress_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        box=box.SIMPLE_HEAD,  # Use simple header with horizontal lines
        title=None,  # Remove the redundant Progress title
        padding=(0, 2),  # Add horizontal padding
        expand=True,  # Make table expand to fill available space
    )
    progress_table.add_column("Team", style="cyan", justify="center", width=20)
    progress_table.add_column("Agent", style="green", justify="center", width=20)
    progress_table.add_column("Status", style="yellow", justify="center", width=20)

    # Group agents by team
    teams = {
        "Analyst Team": [
            "Market Analyst",
            "Social Analyst",
            "News Analyst",
            "Fundamentals Analyst",
        ],
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": ["Risky Analyst", "Neutral Analyst", "Safe Analyst"],
        "Portfolio Management": ["Portfolio Manager"],
    }

    for team, agents in teams.items():
        # Add first agent with team name
        first_agent = agents[0]
        status = message_buffer.agent_status[first_agent]
        if status == "in_progress":
            spinner = Spinner(
                "dots", text="[blue]in_progress[/blue]", style="bold cyan"
            )
            status_cell = spinner
        else:
            status_color = {
                "pending": "yellow",
                "completed": "green",
                "error": "red",
            }.get(status, "white")
            status_cell = f"[{status_color}]{status}[/{status_color}]"
        progress_table.add_row(team, first_agent, status_cell)

        # Add remaining agents in team
        for agent in agents[1:]:
            status = message_buffer.agent_status[agent]
            if status == "in_progress":
                spinner = Spinner(
                    "dots", text="[blue]in_progress[/blue]", style="bold cyan"
                )
                status_cell = spinner
            else:
                status_color = {
                    "pending": "yellow",
                    "completed": "green",
                    "error": "red",
                }.get(status, "white")
                status_cell = f"[{status_color}]{status}[/{status_color}]"
            progress_table.add_row("", agent, status_cell)

        # Add horizontal line after each team
        progress_table.add_row("─" * 20, "─" * 20, "─" * 20, style="dim")

    layout["progress"].update(
        Panel(progress_table, title="Progress", border_style="cyan", padding=(1, 2))
    )

    # Messages panel showing recent messages and tool calls
    messages_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        expand=True,  # Make table expand to fill available space
        box=box.MINIMAL,  # Use minimal box style for a lighter look
        show_lines=True,  # Keep horizontal lines
        padding=(0, 1),  # Add some padding between columns
    )
    messages_table.add_column("Time", style="cyan", width=8, justify="center")
    messages_table.add_column("Type", style="green", width=10, justify="center")
    messages_table.add_column(
        "Content", style="white", no_wrap=False, ratio=1
    )  # Make content column expand

    # Combine tool calls and messages
    all_messages = []

    # Add tool calls
    for timestamp, tool_name, args in message_buffer.tool_calls:
        # Truncate tool call args if too long
        if isinstance(args, str) and len(args) > 100:
            args = args[:97] + "..."
        all_messages.append((timestamp, "Tool", f"{tool_name}: {args}"))

    # Add regular messages
    for timestamp, msg_type, content in message_buffer.messages:
        # Truncate message content if too long
        if isinstance(content, str) and len(content) > 200:
            content = content[:197] + "..."
        all_messages.append((timestamp, msg_type, content))

    # Sort by timestamp
    all_messages.sort(key=lambda x: x[0])

    # Calculate how many messages we can show based on available space
    # Start with a reasonable number and adjust based on content length
    max_messages = 12  # Increased from 8 to better fill the space

    # Get the last N messages that will fit in the panel
    recent_messages = all_messages[-max_messages:]

    # Add messages to table
    for timestamp, msg_type, content in recent_messages:
        # Format content with word wrapping
        wrapped_content = Text(content, overflow="fold")
        messages_table.add_row(timestamp, msg_type, wrapped_content)

    if spinner_text:
        messages_table.add_row("", "Spinner", spinner_text)

    # Add a footer to indicate if messages were truncated
    if len(all_messages) > max_messages:
        messages_table.footer = (
            f"[dim]Showing last {max_messages} of {len(all_messages)} messages[/dim]"
        )

    layout["messages"].update(
        Panel(
            messages_table,
            title="Messages & Tools",
            border_style="blue",
            padding=(1, 2),
        )
    )

    # Analysis panel showing current report
    if message_buffer.current_report:
        layout["analysis"].update(
            Panel(
                Markdown(message_buffer.current_report),
                title="Current Report",
                border_style="green",
                padding=(1, 2),
            )
        )
    else:
        layout["analysis"].update(
            Panel(
                "[italic]Waiting for analysis report...[/italic]",
                title="Current Report",
                border_style="green",
                padding=(1, 2),
            )
        )

    # Footer with statistics
    tool_calls_count = len(message_buffer.tool_calls)
    llm_calls_count = sum(
        1 for _, msg_type, _ in message_buffer.messages if msg_type == "Reasoning"
    )
    reports_count = sum(
        1 for content in message_buffer.report_sections.values() if content is not None
    )

    stats_table = Table(show_header=False, box=None, padding=(0, 2), expand=True)
    stats_table.add_column("Stats", justify="center")
    stats_table.add_row(
        f"Tool Calls: {tool_calls_count} | LLM Calls: {llm_calls_count} | Generated Reports: {reports_count}"
    )

    layout["footer"].update(Panel(stats_table, border_style="grey50"))


def get_user_selections():
    """Get user selections for analysis."""
    # Display ASCII art welcome message
    with open("./cli/static/welcome.txt", "r", encoding="utf-8") as f:
        welcome_ascii = f.read()

    # Create welcome box content
    welcome_content = f"{welcome_ascii}\n"
    welcome_content += "[bold green]AlpacaTradingAgent: Auditable Multi-Agent Trading Research Framework - CLI[/bold green]\n\n"
    welcome_content += "[bold]Workflow Steps:[/bold]\n"
    welcome_content += "I. Analyst Team → II. Research Team → III. Trader → IV. Risk Management → V. Portfolio Management\n\n"
    welcome_content += (
        "[dim]Built by [Tauric Research](https://github.com/TauricResearch)[/dim]"
    )

    # Create and center the welcome box
    welcome_box = Panel(
        welcome_content,
        border_style="green",
        padding=(1, 2),
        title="Welcome to AlpacaTradingAgent",
        subtitle="Paper trading, strategy testing, and risk-controlled execution",
    )
    console.print(Align.center(welcome_box))
    console.print()  # Add a blank line after the welcome box

    def create_question_box(title, prompt, default=None):
        lines = [
            "┌─────────────────────────────────────────────────────────────────────────────────┐",
            f"│ {title:<79} │",
            "├─────────────────────────────────────────────────────────────────────────────────┤",
            f"│ {prompt:<79} │",
        ]
        if default:
            lines.append(f"│ {'Default: ' + default:<79} │")
        lines.append(
            "└─────────────────────────────────────────────────────────────────────────────────┘"
        )
        return "\n".join(lines)

    # Step 1: Ticker symbol
    console.print(
        create_question_box(
            "Step 1: Ticker Symbol", "Enter the ticker symbol", "SPY"
        )
    )
    selected_ticker = get_ticker()

    # Step 2: Use current date for real-time analysis
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")
    console.print(
        f"[green]Using current date for real-time analysis:[/green] {current_date}"
    )

    # Step 3: Select analysts
    console.print(
        create_question_box(
            "Step 3: Analysts Team", "Select your LLM analyst agents for the analysis"
        )
    )
    selected_analysts = select_analysts()
    console.print(
        f"[green]Selected analysts:[/green] {', '.join(analyst.value for analyst in selected_analysts)}"
    )

    # Step 4: Research depth
    console.print(
        create_question_box(
            "Step 4: Research Depth", "Select your research depth level"
        )
    )
    selected_research_depth = select_research_depth()

    # Step 5: Thinking agents
    console.print(
        create_question_box(
            "Step 5: Thinking Agents", "Select your thinking agents for analysis"
        )
    )
    selected_llm_provider = select_llm_provider()
    backend_url = get_backend_url() if selected_llm_provider in {
        "local_openai",
        "ollama",
        "openrouter",
        "azure",
        "xai",
        "minimax",
        "deepseek",
        "qwen",
        "glm",
    } else ""
    selected_shallow_thinker = select_shallow_thinking_agent(selected_llm_provider)
    selected_deep_thinker = select_deep_thinking_agent(selected_llm_provider)
    google_thinking_level = ask_gemini_thinking_config() if selected_llm_provider == "google" else ""
    anthropic_effort = ask_anthropic_effort() if selected_llm_provider == "anthropic" else ""

    # Step 5b: Optional Phase B fixed roles (Analysis / Decision)
    console.print(
        create_question_box(
            "Step 5b: LLM Roles (optional)",
            "Configure Analysis/Decision role overrides? (Analysis serves all "
            "research nodes; Decision serves only the Risk Manager)",
            "no",
        )
    )
    role_settings = {}
    if typer.confirm("Configure role overrides?", default=False):
        from tradingagents.llm_clients.roles import SUPPORTED_PROVIDERS

        def _ask_role(role_label):
            provider = typer.prompt(
                f"{role_label} provider (empty = inherit)",
                default="",
                show_default=False,
            ).strip()
            model = typer.prompt(
                f"{role_label} model (empty = inherit)", default="", show_default=False
            ).strip()
            url = typer.prompt(
                f"{role_label} endpoint override (empty = inherit)",
                default="",
                show_default=False,
            ).strip()
            if provider and provider not in SUPPORTED_PROVIDERS:
                raise typer.BadParameter(
                    f"Unsupported provider {provider!r} "
                    f"(supported: {', '.join(SUPPORTED_PROVIDERS)})"
                )
            return provider, model, url

        analysis_provider, analysis_model, analysis_url = _ask_role("Analysis")
        decision_provider, decision_model, decision_url = _ask_role("Decision")
        role_settings = {
            "analysis_provider": analysis_provider or None,
            "analysis_model": analysis_model or None,
            "analysis_backend_url": analysis_url or None,
            "decision_provider": decision_provider or None,
            "decision_model": decision_model or None,
            "decision_backend_url": decision_url or None,
        }
        # Fail fast on invalid combinations (e.g. cross-provider without model).
        from tradingagents.llm_clients.roles import resolve_role_config

        resolve_role_config(
            {"llm_provider": selected_llm_provider, "backend_url": backend_url or None, **role_settings}
        )

    # Step 5c: Optional Phase C full-market auto screening (Top20)
    console.print(
        create_question_box(
            "Step 5c: Auto Screening (optional)",
            "Enable the daily full-market screen? Screening derives a "
            "validated Top20 and analyzes Top20 plus current holdings; "
            "leave off to analyze the manual ticker above.",
            "no",
        )
    )
    screening_settings = {}
    if typer.confirm("Enable auto screening?", default=False):
        from tradingagents.screening.llm import resolve_screening_config

        screening_provider = (
            typer.prompt("Screening provider", default="", show_default=False).strip() or None
        )
        screening_model = (
            typer.prompt("Screening model", default="", show_default=False).strip() or None
        )
        screening_url = (
            typer.prompt(
                "Screening endpoint override (empty = provider default)",
                default="",
                show_default=False,
            ).strip()
            or None
        )
        screening_refresh = typer.confirm(
            "Refresh: force a new scan even if today's selection exists?", default=False
        )
        screening_settings = {
            "auto_screening_enabled": True,
            "screening_provider": screening_provider,
            "screening_model": screening_model,
            "screening_backend_url": screening_url,
            "screening_refresh": screening_refresh,
        }
        # Fail fast on an incomplete Screening role (no silent inheritance).
        probe = {"llm_provider": selected_llm_provider, **screening_settings}
        try:
            resolve_screening_config(probe)
        except ValueError as exc:
            raise typer.BadParameter(f"Invalid screening configuration: {exc}")

    checkpoint_enabled = select_checkpoint_enabled()
    output_language = get_output_language()

    return {
        "ticker": selected_ticker,
        "analysis_date": current_date,  # Always use current date
        "analysts": selected_analysts,
        "research_depth": selected_research_depth,
        "llm_provider": selected_llm_provider,
        "backend_url": backend_url,
        "checkpoint_enabled": checkpoint_enabled,
        "output_language": output_language,
        **screening_settings,
        "google_thinking_level": google_thinking_level,
        "anthropic_effort": anthropic_effort,
        "shallow_thinker": selected_shallow_thinker,
        "deep_thinker": selected_deep_thinker,
        **role_settings,
    }


def get_ticker():
    """Get ticker symbol from user input."""
    return typer.prompt("", default="SPY")


def display_complete_report(final_state):
    """Display the complete analysis report with team-based panels."""
    console.print("\n[bold green]Complete Analysis Report[/bold green]\n")

    # I. Analyst Team Reports
    analyst_reports = []

    # Market Analyst Report
    if final_state.get("market_report"):
        analyst_reports.append(
            Panel(
                Markdown(final_state["market_report"]),
                title="Market Analyst",
                border_style="blue",
                padding=(1, 2),
            )
        )

    # Social Analyst Report
    if final_state.get("sentiment_report"):
        analyst_reports.append(
            Panel(
                Markdown(final_state["sentiment_report"]),
                title="Social Analyst",
                border_style="blue",
                padding=(1, 2),
            )
        )

    # News Analyst Report
    if final_state.get("news_report"):
        analyst_reports.append(
            Panel(
                Markdown(final_state["news_report"]),
                title="News Analyst",
                border_style="blue",
                padding=(1, 2),
            )
        )

    # Fundamentals Analyst Report
    if final_state.get("fundamentals_report"):
        analyst_reports.append(
            Panel(
                Markdown(final_state["fundamentals_report"]),
                title="Fundamentals Analyst",
                border_style="blue",
                padding=(1, 2),
            )
        )

    # Macro Analyst Report
    if final_state.get("macro_report"):
        analyst_reports.append(
            Panel(
                Markdown(final_state["macro_report"]),
                title="Macro Analyst",
                border_style="blue",
                padding=(1, 2),
            )
        )

    if analyst_reports:
        console.print(
            Panel(
                Columns(analyst_reports, equal=True, expand=True),
                title="I. Analyst Team Reports",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    # II. Research Team Reports
    if final_state.get("investment_debate_state"):
        research_reports = []
        debate_state = final_state["investment_debate_state"]

        # Bull Researcher Analysis
        if debate_state.get("bull_history"):
            research_reports.append(
                Panel(
                    Markdown(debate_state["bull_history"]),
                    title="Bull Researcher",
                    border_style="blue",
                    padding=(1, 2),
                )
            )

        # Bear Researcher Analysis
        if debate_state.get("bear_history"):
            research_reports.append(
                Panel(
                    Markdown(debate_state["bear_history"]),
                    title="Bear Researcher",
                    border_style="blue",
                    padding=(1, 2),
                )
            )

        # Research Manager Decision
        if debate_state.get("judge_decision"):
            research_reports.append(
                Panel(
                    Markdown(debate_state["judge_decision"]),
                    title="Research Manager",
                    border_style="blue",
                    padding=(1, 2),
                )
            )

        if research_reports:
            console.print(
                Panel(
                    Columns(research_reports, equal=True, expand=True),
                    title="II. Research Team Decision",
                    border_style="magenta",
                    padding=(1, 2),
                )
            )

    # III. Trading Team Reports
    if final_state.get("trader_investment_plan"):
        console.print(
            Panel(
                Panel(
                    Markdown(final_state["trader_investment_plan"]),
                    title="Trader",
                    border_style="blue",
                    padding=(1, 2),
                ),
                title="III. Trading Team Plan",
                border_style="yellow",
                padding=(1, 2),
            )
        )

    # IV. Risk Management Team Reports
    if final_state.get("risk_debate_state"):
        risk_reports = []
        risk_state = final_state["risk_debate_state"]

        # Aggressive (Risky) Analyst Analysis
        if risk_state.get("risky_history"):
            risk_reports.append(
                Panel(
                    Markdown(risk_state["risky_history"]),
                    title="Aggressive Analyst",
                    border_style="blue",
                    padding=(1, 2),
                )
            )

        # Conservative (Safe) Analyst Analysis
        if risk_state.get("safe_history"):
            risk_reports.append(
                Panel(
                    Markdown(risk_state["safe_history"]),
                    title="Conservative Analyst",
                    border_style="blue",
                    padding=(1, 2),
                )
            )

        # Neutral Analyst Analysis
        if risk_state.get("neutral_history"):
            risk_reports.append(
                Panel(
                    Markdown(risk_state["neutral_history"]),
                    title="Neutral Analyst",
                    border_style="blue",
                    padding=(1, 2),
                )
            )

        if risk_reports:
            console.print(
                Panel(
                    Columns(risk_reports, equal=True, expand=True),
                    title="IV. Risk Management Team Decision",
                    border_style="red",
                    padding=(1, 2),
                )
            )

        # V. Portfolio Manager Decision
        if risk_state.get("judge_decision"):
            console.print(
                Panel(
                    Panel(
                        Markdown(risk_state["judge_decision"]),
                        title="Portfolio Manager",
                        border_style="blue",
                        padding=(1, 2),
                    ),
                    title="V. Portfolio Manager Decision",
                    border_style="green",
                    padding=(1, 2),
                )
            )


def update_research_team_status(status):
    """Update status for all research team members and trader."""
    research_team = ["Bull Researcher", "Bear Researcher", "Research Manager", "Trader"]
    for agent in research_team:
        message_buffer.update_agent_status(agent, status)


def run_analysis():
    # First get all user selections
    selections = get_user_selections()

    # Create config with selected research depth
    config = DEFAULT_CONFIG.copy()
    config["max_debate_rounds"] = selections["research_depth"]
    config["max_risk_discuss_rounds"] = selections["research_depth"]
    config["quick_think_llm"] = selections["shallow_thinker"]
    config["deep_think_llm"] = selections["deep_thinker"]
    config["llm_provider"] = selections["llm_provider"]
    config["backend_url"] = selections["backend_url"] or None
    config["checkpoint_enabled"] = selections["checkpoint_enabled"]
    config["output_language"] = selections["output_language"]
    if selections.get("google_thinking_level"):
        config["google_thinking_level"] = selections["google_thinking_level"]
    if selections.get("anthropic_effort"):
        config["anthropic_effort"] = selections["anthropic_effort"]
    for role_key in (
        "analysis_provider",
        "analysis_model",
        "analysis_backend_url",
        "decision_provider",
        "decision_model",
        "decision_backend_url",
        "auto_screening_enabled",
        "screening_provider",
        "screening_model",
        "screening_backend_url",
    ):
        if selections.get(role_key) is not None:
            config[role_key] = selections[role_key]
    config["trading_mode"] = "investment"

    # Phase C: auto-screening mode runs the real pipeline (scan or
    # same-day cache, fresh holdings union) and analyzes the whole deep
    # set serially. A screening stop ends the run with zero analysis.
    if config.get("auto_screening_enabled"):
        from tradingagents.screening.pipeline import prepare_screening_round

        plan = prepare_screening_round(config, refresh=bool(selections.get("screening_refresh")))
        if plan.stopped:
            console.print(
                f"[bold red]Screening stopped — {plan.stop_reason_text()}[/bold red]. "
                "No analysis was run; retry the selection explicitly."
            )
            return
        _display_screening_plan(plan)
        tickers = plan.deep_analysis_set
    else:
        tickers = [selections["ticker"]]

    for ticker in tickers:
        console.rule(f"[bold]Analyzing {ticker}")
        _run_cli_analysis_for_ticker(selections, config, ticker)


def _display_screening_plan(plan):
    """Print the Top20 selection, reasons and round composition."""
    from rich.table import Table

    console.print(
        f"[bold green]Screening selection[/bold green] trading date "
        f"{plan.selection_date} (as_of {plan.as_of}, "
        f"{'cached' if plan.cached else 'fresh scan'})"
    )
    table = Table(title="Top20 (research priority)")
    table.add_column("Rank", justify="right")
    table.add_column("Symbol")
    table.add_column("Score", justify="right")
    table.add_column("Reason")
    for entry in plan.top20:
        table.add_row(
            str(entry.get("rank")),
            str(entry.get("symbol")),
            f"{float(entry.get('screening_score') or 0):.1f}",
            str(entry.get("short_reason", ""))[:120],
        )
    console.print(table)
    if plan.overlap_holdings:
        console.print(f"Top20 ∩ holdings: {', '.join(plan.overlap_holdings)}")
    if plan.extra_holdings:
        console.print(f"Held review (not in Top20): {', '.join(plan.extra_holdings)}")
    for blocked in plan.blocked_holdings:
        console.print(
            f"[yellow]Blocked held review: {blocked['symbol']} — {blocked['reason']}[/yellow]"
        )
    if plan.other_asset_holdings:
        console.print(
            "Other-asset holdings (existing management path): "
            + ", ".join(h["symbol"] for h in plan.other_asset_holdings)
        )
    if plan.mode == "held_review":
        console.print(
            "[yellow]Non-trading day: held-risk review only — no scan, no new entries.[/yellow]"
        )

def _run_cli_analysis_for_ticker(selections, config, ticker):
    """Run the existing single-ticker CLI analysis flow for one symbol."""
    # Initialize the graph
    graph = TradingAgentsGraph(
        [analyst.value for analyst in selections["analysts"]], config=config, debug=True
    )
    run_logger = get_run_audit_logger()
    run_started = False

    # Now start the display layout
    layout = create_layout()

    with Live(layout, refresh_per_second=4) as live:
        # Initial display
        update_display(layout)

        # Add initial messages
        message_buffer.add_message("System", f"Selected ticker: {selections['ticker']}")
        message_buffer.add_message(
            "System", f"Analysis date: {selections['analysis_date']}"
        )
        message_buffer.add_message(
            "System",
            f"Selected analysts: {', '.join(analyst.value for analyst in selections['analysts'])}",
        )
        update_display(layout)

        # Reset agent statuses
        for agent in message_buffer.agent_status:
            message_buffer.update_agent_status(agent, "pending")

        # Reset report sections
        for section in message_buffer.report_sections:
            message_buffer.report_sections[section] = None
        message_buffer.current_report = None
        message_buffer.final_report = None

        # Update agent status to in_progress for the first analyst
        first_analyst = f"{selections['analysts'][0].value.capitalize()} Analyst"
        message_buffer.update_agent_status(first_analyst, "in_progress")
        update_display(layout)

        # Create spinner text
        spinner_text = (
            f"Analyzing {selections['ticker']} on {selections['analysis_date']}..."
        )
        update_display(layout, spinner_text)

        # Initialize state and get graph args
        init_agent_state = graph.propagator.create_initial_state(
            ticker, selections["analysis_date"]
        )
        graph._resolve_memory_log_outcomes(ticker, selections["analysis_date"])
        args = graph._graph_args_for_run(ticker, selections["analysis_date"])
        compiled_graph, checkpointer_ctx = graph._graph_for_run(
            ticker, selections["analysis_date"]
        )
        run_logger.start_run(
            symbol=ticker,
            trade_date=str(selections["analysis_date"]),
            config=config,
            metadata={"debug": True, "source": "cli_stream"},
        )
        run_started = True
        run_logger.log_state_snapshot(
            stage="initial_state",
            snapshot=init_agent_state,
            symbol=ticker,
        )

        # Stream the analysis
        trace = []
        try:
            try:
                graph_stream = compiled_graph.stream(init_agent_state, **args)
                for chunk in graph_stream:
                    if len(chunk["messages"]) > 0:
                        # Get the last message from the chunk
                        last_message = chunk["messages"][-1]

                        # Extract message content and type
                        if hasattr(last_message, "content"):
                            content = last_message.content
                            msg_type = "Reasoning"
                        else:
                            content = str(last_message)
                            msg_type = "System"

                        # Add message to buffer
                        message_buffer.add_message(msg_type, content)

                        # If it's a tool call, add it to tool calls
                        if hasattr(last_message, "tool_calls"):
                            for tool_call in last_message.tool_calls:
                                # Handle both dictionary and object tool calls
                                if isinstance(tool_call, dict):
                                    message_buffer.add_tool_call(
                                        tool_call["name"], tool_call["args"]
                                    )
                                else:
                                    message_buffer.add_tool_call(tool_call.name, tool_call.args)

                        # Update reports and agent status based on chunk content
                        # Analyst Team Reports
                        if "market_report" in chunk and chunk["market_report"]:
                            message_buffer.update_report_section(
                                "market_report", chunk["market_report"]
                            )
                            message_buffer.update_agent_status("Market Analyst", "completed")
                            # Set next analyst to in_progress
                            if "social" in selections["analysts"]:
                                message_buffer.update_agent_status(
                                    "Social Analyst", "in_progress"
                                )

                        if "sentiment_report" in chunk and chunk["sentiment_report"]:
                            message_buffer.update_report_section(
                                "sentiment_report", chunk["sentiment_report"]
                            )
                            message_buffer.update_agent_status("Social Analyst", "completed")
                            # Set next analyst to in_progress
                            if "news" in selections["analysts"]:
                                message_buffer.update_agent_status(
                                    "News Analyst", "in_progress"
                                )

                        if "news_report" in chunk and chunk["news_report"]:
                            message_buffer.update_report_section(
                                "news_report", chunk["news_report"]
                            )
                            message_buffer.update_agent_status("News Analyst", "completed")
                            # Set next analyst to in_progress
                            if "fundamentals" in selections["analysts"]:
                                message_buffer.update_agent_status(
                                    "Fundamentals Analyst", "in_progress"
                                )

                        if "fundamentals_report" in chunk and chunk["fundamentals_report"]:
                            message_buffer.update_report_section(
                                "fundamentals_report", chunk["fundamentals_report"]
                            )
                            message_buffer.update_agent_status(
                                "Fundamentals Analyst", "completed"
                            )
                            if "macro" in selections["analysts"]:
                                message_buffer.update_agent_status(
                                    "Macro Analyst", "in_progress"
                                )

                        if "macro_report" in chunk and chunk["macro_report"]:
                            message_buffer.update_report_section(
                                "macro_report", chunk["macro_report"]
                            )
                            message_buffer.update_agent_status("Macro Analyst", "completed")
                            update_research_team_status("in_progress")

                    # Research Team - Handle Investment Debate State
                    if (
                        "investment_debate_state" in chunk
                        and chunk["investment_debate_state"]
                    ):
                        debate_state = chunk["investment_debate_state"]

                        # Update Bull Researcher status and report
                        if "bull_history" in debate_state and debate_state["bull_history"]:
                            # Keep all research team members in progress
                            update_research_team_status("in_progress")
                            # Extract latest bull response
                            bull_responses = debate_state["bull_history"].split("\n")
                            latest_bull = bull_responses[-1] if bull_responses else ""
                            if latest_bull:
                                message_buffer.add_message("Reasoning", latest_bull)
                                # Update research report with bull's latest analysis
                                message_buffer.update_report_section(
                                    "investment_plan",
                                    f"### Bull Researcher Analysis\n{latest_bull}",
                                )

                        # Update Bear Researcher status and report
                        if "bear_history" in debate_state and debate_state["bear_history"]:
                            # Keep all research team members in progress
                            update_research_team_status("in_progress")
                            # Extract latest bear response
                            bear_responses = debate_state["bear_history"].split("\n")
                            latest_bear = bear_responses[-1] if bear_responses else ""
                            if latest_bear:
                                message_buffer.add_message("Reasoning", latest_bear)
                                # Update research report with bear's latest analysis
                                message_buffer.update_report_section(
                                    "investment_plan",
                                    f"{message_buffer.report_sections['investment_plan']}\n\n### Bear Researcher Analysis\n{latest_bear}",
                                )

                        # Update Research Manager status and final decision
                        if (
                            "judge_decision" in debate_state
                            and debate_state["judge_decision"]
                        ):
                            # Keep all research team members in progress until final decision
                            update_research_team_status("in_progress")
                            message_buffer.add_message(
                                "Reasoning",
                                f"Research Manager: {debate_state['judge_decision']}",
                            )
                            # Update research report with final decision
                            message_buffer.update_report_section(
                                "investment_plan",
                                f"{message_buffer.report_sections['investment_plan']}\n\n### Research Manager Decision\n{debate_state['judge_decision']}",
                            )
                            # Mark all research team members as completed
                            update_research_team_status("completed")
                            # Set first risk analyst to in_progress
                            message_buffer.update_agent_status(
                                "Risky Analyst", "in_progress"
                            )

                    # Trading Team
                    if (
                        "trader_investment_plan" in chunk
                        and chunk["trader_investment_plan"]
                    ):
                        message_buffer.update_report_section(
                            "trader_investment_plan", chunk["trader_investment_plan"]
                        )
                        # Set first risk analyst to in_progress
                        message_buffer.update_agent_status("Risky Analyst", "in_progress")

                    # Risk Management Team - Handle Risk Debate State
                    if "risk_debate_state" in chunk and chunk["risk_debate_state"]:
                        risk_state = chunk["risk_debate_state"]

                        # Update Risky Analyst status and report
                        if (
                            "current_risky_response" in risk_state
                            and risk_state["current_risky_response"]
                        ):
                            message_buffer.update_agent_status(
                                "Risky Analyst", "in_progress"
                            )
                            message_buffer.add_message(
                                "Reasoning",
                                f"Risky Analyst: {risk_state['current_risky_response']}",
                            )
                            # Update risk report with risky analyst's latest analysis only
                            message_buffer.update_report_section(
                                "final_trade_decision",
                                f"### Risky Analyst Analysis\n{risk_state['current_risky_response']}",
                            )

                        # Update Safe Analyst status and report
                        if (
                            "current_safe_response" in risk_state
                            and risk_state["current_safe_response"]
                        ):
                            message_buffer.update_agent_status(
                                "Safe Analyst", "in_progress"
                            )
                            message_buffer.add_message(
                                "Reasoning",
                                f"Safe Analyst: {risk_state['current_safe_response']}",
                            )
                            # Update risk report with safe analyst's latest analysis only
                            message_buffer.update_report_section(
                                "final_trade_decision",
                                f"### Safe Analyst Analysis\n{risk_state['current_safe_response']}",
                            )

                        # Update Neutral Analyst status and report
                        if (
                            "current_neutral_response" in risk_state
                            and risk_state["current_neutral_response"]
                        ):
                            message_buffer.update_agent_status(
                                "Neutral Analyst", "in_progress"
                            )
                            message_buffer.add_message(
                                "Reasoning",
                                f"Neutral Analyst: {risk_state['current_neutral_response']}",
                            )
                            # Update risk report with neutral analyst's latest analysis only
                            message_buffer.update_report_section(
                                "final_trade_decision",
                                f"### Neutral Analyst Analysis\n{risk_state['current_neutral_response']}",
                            )

                        # Update Portfolio Manager status and final decision
                        if "judge_decision" in risk_state and risk_state["judge_decision"]:
                            message_buffer.update_agent_status(
                                "Portfolio Manager", "in_progress"
                            )
                            message_buffer.add_message(
                                "Reasoning",
                                f"Portfolio Manager: {risk_state['judge_decision']}",
                            )
                            # Update risk report with final decision only
                            message_buffer.update_report_section(
                                "final_trade_decision",
                                f"### Portfolio Manager Decision\n{risk_state['judge_decision']}",
                            )
                            # Mark risk analysts as completed
                            message_buffer.update_agent_status("Risky Analyst", "completed")
                            message_buffer.update_agent_status("Safe Analyst", "completed")
                            message_buffer.update_agent_status(
                                "Neutral Analyst", "completed"
                            )
                            message_buffer.update_agent_status(
                                "Portfolio Manager", "completed"
                            )

                        # Update the display
                        update_display(layout)

                    trace.append(chunk)
            finally:
                if checkpointer_ctx is not None:
                    checkpointer_ctx.__exit__(None, None, None)

            # Get final state and decision
            final_state = trace[-1]
            decision = trade_intent_action(final_state.get("final_trade_intent")) or graph.process_signal(
                final_state["final_trade_decision"]
            )
            graph.curr_state = final_state
            graph.ticker = ticker
            graph._log_state(selections["analysis_date"], final_state)
            run_logger.finish_run(
                symbol=ticker,
                status="completed",
                final_state=final_state,
                final_signal=decision,
            )
            graph.memory_log.store_decision(
                ticker=ticker,
                trade_date=selections["analysis_date"],
                final_trade_decision=final_state["final_trade_decision"],
                trading_mode=final_state.get("trading_mode", config.get("trading_mode", "investment")),
            )
            if config.get("checkpoint_enabled", False):
                clear_checkpoint(
                    config["data_cache_dir"],
                    ticker,
                    selections["analysis_date"],
                )
            run_started = False
        except ProviderFailure as e:
            # Phase B: provider access failure stops the run; make it visible
            # in the CLI as a distinct stopped state (already logged by the
            # run audit trail with role/model/attempts detail).
            if run_started:
                run_logger.finish_run(
                    symbol=ticker,
                    status="stopped",
                    final_state=trace[-1] if trace else None,
                    error_message=str(e),
                )
                run_started = False
            console.print(f"[bold red]Run stopped — provider failure:[/bold red] {e}")
            raise
        except Exception as e:
            if run_started:
                run_logger.finish_run(
                    symbol=ticker,
                    status="failed",
                    final_state=trace[-1] if trace else None,
                    error_message=str(e),
                )
                run_started = False
            raise

        # Update all agent statuses to completed
        for agent in message_buffer.agent_status:
            message_buffer.update_agent_status(agent, "completed")

        message_buffer.add_message(
            "Analysis", f"Completed analysis for {selections['analysis_date']}"
        )

        # Update final report sections
        for section in message_buffer.report_sections.keys():
            if section in final_state:
                message_buffer.update_report_section(section, final_state[section])

        # Display the complete final report
        display_complete_report(final_state)

        update_display(layout)


@app.command()
def analyze():
    run_analysis()


# ---------------------------------------------------------------------------
# Phase D: 30-day unattended paper observation
# ---------------------------------------------------------------------------

_PROVIDER_STANDARD_ENV = {
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "xai": "XAI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
}


def _long_run_env_path() -> str:
    import os as _os

    return _os.path.join(_os.getcwd(), ".env")


def _read_env_file(path: str) -> tuple[list[str], dict[str, str]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return [], {}
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key:
            values[key] = value.strip().strip("\"'")
    return lines, values


def _write_env_updates(path: str, updates: dict[str, str]) -> None:
    import os as _os

    lines, _ = _read_env_file(path)
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = None
        if stripped and not stripped.startswith("#") and "=" in stripped:
            candidate, _, _ = stripped.partition("=")
            candidate = candidate.strip()
            if candidate.startswith("export "):
                candidate = candidate[len("export "):].strip()
            key = candidate
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out) + "\n")
    for key, value in updates.items():
        _os.environ[key] = value


def _prompt_secret(label: str) -> str:
    import getpass as _getpass

    try:
        value = _getpass.getpass(f"{label} (hidden input): ").strip()
    except (EOFError, KeyboardInterrupt):
        raise typer.Abort()
    if not value:
        console.print("[bold red]A value is required; aborting.[/bold red]")
        raise typer.Abort()
    return value


def collect_long_run_config(existing: dict) -> tuple[dict, dict[str, str]]:
    """Prompt only for missing/invalid Phase-D values. Returns (config, secrets)."""
    import sys as _sys

    from tradingagents.llm_clients.roles import SUPPORTED_PROVIDERS

    if not _sys.stdin.isatty():
        missing = __import__(
            "tradingagents.long_run", fromlist=["missing_config_fields"]
        ).missing_config_fields(existing)
        if missing:
            console.print(
                "[bold red]Missing Phase-D settings "
                f"({', '.join(missing)}) and no interactive terminal. "
                "Re-run interactively to configure.[/bold red]"
            )
            raise typer.Exit(code=2)
        return existing, {}

    cfg = dict(existing)
    secrets: dict[str, str] = {}

    def _ask_provider(role: str) -> None:
        nonlocal cfg
        current = (cfg.get(f"{role}_provider") or "").strip()
        if current in SUPPORTED_PROVIDERS:
            return
        console.print(f"[bold]{role.capitalize()} role provider[/bold] (supported: "
                      + ", ".join(SUPPORTED_PROVIDERS) + ")")
        while True:
            value = typer.prompt(f"{role} provider",
                                 default=current or "").strip()
            if value in SUPPORTED_PROVIDERS:
                cfg[f"{role}_provider"] = value
                return
            console.print(f"[red]Unsupported provider {value!r}.[/red]")

    def _ask_model(role: str) -> None:
        nonlocal cfg
        current = (cfg.get(f"{role}_model") or "").strip()
        if current:
            return
        cfg[f"{role}_model"] = typer.prompt(f"{role} model").strip()

    def _ask_url(role: str) -> None:
        nonlocal cfg
        from tradingagents.long_run import PROVIDERS_REQUIRING_URL

        provider = str(cfg.get(f"{role}_provider") or "").lower()
        current = (cfg.get(f"{role}_backend_url") or "").strip()
        if provider not in PROVIDERS_REQUIRING_URL:
            if not current:
                cfg[f"{role}_backend_url"] = None
            return
        if current:
            return
        cfg[f"{role}_backend_url"] = typer.prompt(
            f"{role} backend URL (required for {provider})").strip()

    for _role in ("analysis", "decision", "screening"):
        _ask_provider(_role)
        _ask_model(_role)
        _ask_url(_role)

    # Optional Analysis fallback (Phase B failover route). All three keys
    # are optional overall: an existing complete pair is preserved and never
    # re-asked; a partial pair is either completed or cleared; when nothing
    # is set, a yes/no (default no) gates the extra prompts.
    from tradingagents.long_run import PROVIDERS_REQUIRING_URL as _FB_URL_PROVIDERS

    fb_provider = (cfg.get("analysis_fallback_provider") or "").strip()
    fb_model = (cfg.get("analysis_fallback_model") or "").strip()
    fb_url = (cfg.get("analysis_fallback_backend_url") or "").strip()
    if fb_provider and fb_model and fb_provider in SUPPORTED_PROVIDERS:
        fb_enabled = True  # complete, valid pair: preserve, never re-ask
    elif fb_provider or fb_model:
        fb_enabled = typer.confirm(
            f"Existing partial Analysis fallback config found "
            f"(provider={fb_provider or '(none)'}); complete it?",
            default=True,
        )
    else:
        fb_enabled = typer.confirm(
            "Enable optional Analysis fallback (a second provider route used "
            "only when the Analysis role fails)?",
            default=False,
        )
    if fb_enabled:
        if fb_provider not in SUPPORTED_PROVIDERS:
            while True:
                value = typer.prompt(
                    "Analysis fallback provider",
                    default=fb_provider or "").strip()
                if value in SUPPORTED_PROVIDERS:
                    fb_provider = value
                    break
                console.print(f"[red]Unsupported provider {value!r}.[/red]")
        while not fb_model:
            fb_model = typer.prompt("Analysis fallback model").strip()
        if fb_provider in _FB_URL_PROVIDERS and not fb_url:
            fb_url = typer.prompt(
                f"Analysis fallback backend URL (required for {fb_provider})").strip()
        elif fb_url == "" and fb_provider != str(
            cfg.get("analysis_provider") or ""
        ).strip().lower():
            # A cross-provider fallback never inherits the Analysis endpoint;
            # ask once (Enter = the provider's default endpoint).
            entered = typer.prompt(
                "Analysis fallback backend URL (optional, Enter = provider default)",
                default="").strip()
            fb_url = entered
        cfg["analysis_fallback_provider"] = fb_provider
        cfg["analysis_fallback_model"] = fb_model
        cfg["analysis_fallback_backend_url"] = fb_url or None
    else:
        cfg["analysis_fallback_provider"] = None
        cfg["analysis_fallback_model"] = None
        cfg["analysis_fallback_backend_url"] = None

    if cfg.get("base_trade_notional_usd") is None:
        while True:
            raw = typer.prompt("Base trade notional in USD (e.g. 1000)").strip()
            try:
                amount = float(raw)
                if amount > 0:
                    cfg["base_trade_notional_usd"] = amount
                    break
            except ValueError:
                pass
            console.print("[red]Enter a positive number.[/red]")

    from tradingagents.long_run import parse_run_time_et

    def _run_time_valid(value) -> bool:
        try:
            parsed = parse_run_time_et(str(value or ""))
        except ValueError:
            return False
        return __import__("datetime").time(9, 30) <= parsed <= __import__("datetime").time(16, 0)

    if not _run_time_valid(cfg.get("run_time_et")):
        # Ask only when missing or invalid; never re-ask a valid value.
        while True:
            raw = typer.prompt("Daily run time (ET, HH:MM)",
                               default=str(cfg.get("run_time_et") or "11:00")).strip()
            try:
                parsed = parse_run_time_et(raw)
                if not (__import__("datetime").time(9, 30) <= parsed
                        <= __import__("datetime").time(16, 0)):
                    console.print("[red]Must be within 09:30-16:00 ET.[/red]")
                    continue
                cfg["run_time_et"] = raw
                break
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")

    if not cfg.get("analysts"):
        console.print("[bold]Analyst team[/bold] (default: all five)")
        if typer.confirm("Customize analyst selection?", default=False):
            from cli.utils import select_analysts as _select

            cfg["analysts"] = [a.value for a in _select()]
        else:
            cfg["analysts"] = ["market", "social", "news", "fundamentals", "macro"]

    if cfg.get("research_depth") not in (1, 3, 5):
        choice = typer.prompt("Research depth (Shallow/Medium/Deep)",
                              default="Medium").strip().lower()
        cfg["research_depth"] = {"shallow": 1, "deep": 5}.get(choice, 3)

    if not (cfg.get("output_language") or "").strip():
        cfg["output_language"] = typer.prompt("Output language",
                                              default="English").strip() or "English"

    # Credentials: one prompt per distinct provider lacking any usable key,
    # plus Alpaca Paper keys. Secrets go to .env only, never to config.json.
    import os as _os

    from tradingagents.dataflows.config import get_llm_api_key
    from tradingagents.llm_clients.roles import _resolve_provider_key

    seen_providers: dict[str, str] = {}
    for _role in ("analysis", "decision", "screening"):
        provider = str(cfg.get(f"{_role}_provider") or "").lower()
        seen_providers.setdefault(provider, _role)
    for provider, _role in seen_providers.items():
        if provider in ("local_openai", "ollama"):
            continue
        has_key = bool(_resolve_provider_key(provider, _role).strip()
                       or (get_llm_api_key(provider) or "").strip())
        env_name = _PROVIDER_STANDARD_ENV.get(provider)
        if not has_key and env_name and not (_os.getenv(env_name) or "").strip():
            console.print(f"[bold]Missing API key for provider {provider!r}.[/bold]")
            secrets[env_name] = _prompt_secret(env_name)
        if provider == "azure" and not (_os.getenv("AZURE_OPENAI_ENDPOINT") or "").strip():
            console.print("[bold]Azure OpenAI endpoint required.[/bold]")
            secrets["AZURE_OPENAI_ENDPOINT"] = typer.prompt(
                "AZURE_OPENAI_ENDPOINT").strip()

    # Analysis fallback credential: the resolver accepts
    # ANALYSIS_FALLBACK_<PROVIDER>_API_KEY or the provider's standard key;
    # prompt only when neither exists (including keys just collected above).
    fb_provider_cfg = str(cfg.get("analysis_fallback_provider") or "").lower()
    if fb_provider_cfg and fb_provider_cfg not in ("local_openai", "ollama"):
        fb_env_name = _PROVIDER_STANDARD_ENV.get(fb_provider_cfg)
        fb_has_key = bool(
            _resolve_provider_key(fb_provider_cfg, "analysis_fallback").strip()
            or (get_llm_api_key(fb_provider_cfg) or "").strip()
            or (fb_env_name and (secrets.get(fb_env_name) or "").strip())
        )
        if not fb_has_key and fb_env_name:
            console.print(
                f"[bold]Missing API key for Analysis fallback provider "
                f"{fb_provider_cfg!r}.[/bold]"
            )
            secrets[fb_env_name] = _prompt_secret(fb_env_name)
        if (
            fb_provider_cfg == "azure"
            and not (_os.getenv("AZURE_OPENAI_ENDPOINT") or "").strip()
            and "AZURE_OPENAI_ENDPOINT" not in secrets
        ):
            console.print("[bold]Azure OpenAI endpoint required.[/bold]")
            secrets["AZURE_OPENAI_ENDPOINT"] = typer.prompt(
                "AZURE_OPENAI_ENDPOINT").strip()
    if not (_os.getenv("ALPACA_API_KEY") or "").strip():
        console.print("[bold]Missing Alpaca Paper credentials.[/bold]")
        secrets["ALPACA_API_KEY"] = _prompt_secret("ALPACA_API_KEY")
    if not (_os.getenv("ALPACA_SECRET_KEY") or "").strip():
        secrets["ALPACA_SECRET_KEY"] = _prompt_secret("ALPACA_SECRET_KEY")
    return cfg, secrets


@app.command("long-run")
def long_run():
    """Configure, start, or resume the 30-day Paper observation (paper-only)."""
    import sys as _sys
    from datetime import date as _date
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta

    from tradingagents import long_run as lr

    if _sys.version_info < (3, 10):
        console.print("[bold red]Phase-D requires Python 3.10+.[/bold red]")
        raise typer.Exit(code=2)

    active = lr.load_active_state()
    if active is not None:
        # Resume path: same window, recovery first, no new prompts.
        try:
            with lr.runner_lock():
                pass
        except lr.RunnerLockBusy:
            console.print("[bold yellow]A Phase-D runner is already active.[/bold yellow]")
            console.print(f"run_id: {active.get('run_id')}")
            console.print(f"status: {active.get('status')}")
            raise typer.Exit(code=2)
        active["restart_count"] = int(active.get("restart_count") or 0) + 1
        lr.save_active_state(active)
        long_cfg = dict(lr.default_long_run_config())
        long_cfg.update(active.get("config") or {})
        runtime = lr.build_runtime_config(long_cfg)
        console.print(f"[green]Resuming observation {active['run_id']} "
                      f"(restart #{active['restart_count']}).[/green]")
        try:
            with lr.runner_lock():
                result = lr.run_observation_loop(active, long_cfg, runtime, lr.LongRunDeps())
        except lr.LongRunStop as exc:
            console.print(f"[bold red]Observation stopped: {exc.code}: {exc.detail}[/bold red]")
            raise typer.Exit(code=1)
        raise typer.Exit(code=0 if result.get("outcome") == "completed" else 1)

    cfg = lr.load_long_run_config()
    cfg, secrets = collect_long_run_config(cfg)
    if secrets:
        _write_env_updates(_long_run_env_path(), secrets)
        console.print("[green]Saved credentials to .env (never printed back).[/green]")
    lr.save_long_run_config(cfg)
    runtime = lr.build_runtime_config(cfg)

    # Read-only preflight before anything is created.
    try:
        preflight = lr.run_preflight(cfg, runtime, lr.LongRunDeps())
    except lr.LongRunStop as exc:
        console.print(f"[bold red]Preflight failed: {exc.code}: {exc.detail}[/bold red]")
        raise typer.Exit(code=1)

    console.print("\n[bold]Phase-D observation summary (secret-free)[/bold]")
    console.print(f"Duration: {cfg['duration_calendar_days']} calendar days "
                  f"| daily at {cfg['run_time_et']} ET "
                  f"| notional ${float(cfg['base_trade_notional_usd']):,.0f}")
    console.print(f"Analysts: {', '.join(cfg['analysts'])} "
                  f"| depth {cfg['research_depth']} | {cfg['output_language']}")
    for _role in ("analysis", "decision", "screening"):
        console.print(f"{_role}: {cfg[f'{_role}_provider']}/{cfg[f'{_role}_model']} "
                      f"endpoint={lr.sanitize_url(cfg.get(f'{_role}_backend_url')) or 'provider default'}")
    if cfg.get("analysis_fallback_provider") and cfg.get("analysis_fallback_model"):
        console.print(
            f"analysis_fallback: {cfg['analysis_fallback_provider']}/"
            f"{cfg['analysis_fallback_model']} "
            f"endpoint={lr.sanitize_url(cfg.get('analysis_fallback_backend_url')) or 'provider default'}"
        )
    console.print("Mode: paper-only, auto_screening_enabled=True, "
                  "allow_shorts=False, trading_mode=investment")
    if not typer.confirm(
        "Authorize this 30-calendar-day PAPER test? The process must stay "
        "running (rerun this command to resume after a crash).",
        default=False,
    ):
        console.print("Not authorized; no observation was created.")
        raise typer.Exit(code=1)

    # Authoritative session list for the window, then create state + RUNNING.
    eastern = lr.eastern_now()
    start_day = eastern.date()
    end_day = start_day + _timedelta(days=int(cfg["duration_calendar_days"]))
    try:
        expected = [d.isoformat() for d in lr.fetch_session_dates(start_day, end_day)]
    except Exception as exc:
        console.print(f"[bold red]Cannot prove observation sessions: {exc}[/bold red]")
        raise typer.Exit(code=1)
    state = lr.new_observation_state(cfg, expected_sessions=expected)
    manifest = {
        "run_id": state["run_id"], "created_at": state["started_at"],
        "starts_at": state["started_at"], "ends_at": state["ends_at"],
        "config": state["config"], "expected_sessions": expected,
        "baseline_commit": state["baseline_commit"],
    }
    lr.atomic_write_json(lr.run_dir(state["run_id"]) / "manifest.json", manifest)
    lr.append_jsonl(lr.run_dir(state["run_id"]) / "account_snapshots.jsonl",
                    {"phase": "startup", **preflight["snapshot"]})
    lr.log_event(state["run_id"], "observation_created",
                 {"expected_sessions": len(expected)})
    lr.save_active_state(state)
    console.print(f"[green]Observation {state['run_id']} entering RUNNING. "
                  "No further input is required.[/green]")
    try:
        with lr.runner_lock():
            result = lr.run_observation_loop(state, cfg, runtime, lr.LongRunDeps())
    except lr.LongRunStop as exc:
        console.print(f"[bold red]Observation stopped: {exc.code}: {exc.detail}[/bold red]")
        raise typer.Exit(code=1)
    raise typer.Exit(code=0 if result.get("outcome") == "completed" else 1)


if __name__ == "__main__":
    app()
