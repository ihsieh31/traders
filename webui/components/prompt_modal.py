"""
Prompt Modal Component for TradingAgents WebUI

This component creates a modal dialog that displays the prompt used by an agent
to generate a specific report.
"""

import dash_bootstrap_components as dbc
from dash import html, dcc


def create_prompt_modal():
    """Create a modal for displaying agent prompts"""
    
    modal = dbc.Modal(
        [
            dbc.ModalHeader(
                [
                    html.H4([
                        html.I(className="fas fa-code me-2"),
                        html.Span(id="prompt-modal-title", children="Agent Prompt")
                    ], className="mb-0"),
                ],
                close_button=True,
                className="prompt-modal-header"
            ),
            dbc.ModalBody(
                [
                    html.Div([
                        html.Div([
                            html.I(className="fas fa-info-circle me-2"),
                            "This is the system prompt that was used to generate the report."
                        ], className="alert alert-info mb-3"),
                        
                        html.Div([
                            dcc.Markdown(
                                id="prompt-modal-content",
                                children="Loading prompt...",
                                className="prompt-content",
                                style={
                                    "background": "#0F172A",
                                    "border": "1px solid #334155",
                                    "border-radius": "8px",
                                    "padding": "1.5rem",
                                    "font-family": "monospace",
                                    "font-size": "14px",
                                    "line-height": "1.5",
                                    "color": "#E2E8F0",
                                    "white-space": "pre-wrap",
                                    "max-height": "400px",
                                    "overflow-y": "auto"
                                }
                            )
                        ])
                    ])
                ],
                className="prompt-modal-body",
                style={"max-height": "70vh", "overflow-y": "auto"}
            ),
            dbc.ModalFooter(
                [
                    # U17: browser clipboard access needs a clientside
                    # callback; the button never actually copied, so it is
                    # disabled instead of faking success feedback.
                    dbc.Button(
                        [
                            html.I(className="fas fa-copy me-2"),
                            "Copy to Clipboard"
                        ],
                        id="copy-prompt-btn",
                        color="outline-primary",
                        size="sm",
                        className="me-2",
                        disabled=True,
                        title="Not implemented — select the prompt text to copy",
                    ),
                    dbc.Button(
                        [
                            html.I(className="fas fa-times me-2"),
                            "Close"
                        ],
                        id="close-prompt-modal-btn",
                        color="secondary",
                        size="sm"
                    )
                ]
            )
        ],
        id="prompt-modal",
        is_open=False,
        size="lg",
        backdrop=True,
        scrollable=True,
        className="prompt-modal",
        style={"z-index": "9999"}
    )
    
    return modal


def create_show_prompt_button(report_type, size="sm", className=""):
    """Create a 'Show Prompt' button for a specific report type"""
    
    button_id = f"show-prompt-{report_type}"
    
    button = dbc.Button(
        [
            html.I(className="fas fa-code me-1"),
            "Show Prompt"
        ],
        id={"type": "show-prompt-btn", "report": report_type},
        color="outline-info",
        size=size,
        className=f"show-prompt-btn {className}",
        title=f"View the prompt used to generate this {report_type.replace('_', ' ').title()}",
        style={
            "opacity": "0.8",
            "font-size": "0.75rem",
            "padding": "0.25rem 0.5rem"
        }
    )
    
    return button


def create_report_header_with_prompt_button(title, report_type, icon_class="fas fa-chart-line"):
    """Create a report header with title and prompt button"""
    
    header = html.Div([
        html.Div([
            html.H5([
                html.I(className=f"{icon_class} me-2"),
                title
            ], className="mb-0 report-header-title"),
        ], className="flex-grow-1"),
        
        html.Div([
            create_show_prompt_button(report_type)
        ], className="report-header-actions")
        
    ], className="d-flex justify-content-between align-items-center mb-3 report-header")
    
    return header 