"""
Tool Outputs Modal Component for TradingAgents WebUI

This component creates a modal dialog that displays the tool calls made by agents
and their outputs, helping users debug and verify tool execution.
"""

import dash_bootstrap_components as dbc
from dash import html, dcc
import json


def create_tool_outputs_modal():
    """Create a modal for displaying tool outputs"""
    
    modal = dbc.Modal(
        [
            dbc.ModalHeader(
                [
                    html.H4([
                        html.I(className="fas fa-tools me-2"),
                        html.Span(id="tool-outputs-modal-title", children="Tool Outputs")
                    ], className="mb-0"),
                ],
                close_button=True,
                className="tool-outputs-modal-header"
            ),
            dbc.ModalBody(
                [
                    html.Div([
                        html.I(className="fas fa-info-circle me-2"),
                        "This shows all tool calls made during the analysis with their inputs and outputs."
                    ], className="alert alert-info mb-3"),
                    
                    html.Div(
                        id="tool-outputs-modal-content",
                        children="Loading tool outputs...",
                        className="tool-outputs-content",
                        style={
                            "background": "#0F172A",
                            "border": "1px solid #334155",
                            "border-radius": "8px",
                            "padding": "1.5rem",
                            "font-family": "monospace",
                            "font-size": "14px",
                            "line-height": "1.5",
                            "color": "#E2E8F0"
                        }
                    )
                ],
                className="tool-outputs-modal-body"
            ),
            dbc.ModalFooter(
                [
                    # U17: never actually copied anything — disabled instead
                    # of faking success feedback.
                    dbc.Button(
                        [
                            html.I(className="fas fa-copy me-2"),
                            "Copy All"
                        ],
                        id="copy-tool-outputs-btn",
                        color="outline-primary",
                        size="sm",
                        className="me-2",
                        disabled=True,
                        title="Not implemented — select the output text to copy",
                    ),
                    # U17: never triggered a download — disabled instead of
                    # faking export success.
                    dbc.Button(
                        [
                            html.I(className="fas fa-download me-2"),
                            "Export JSON"
                        ],
                        id="export-tool-outputs-btn",
                        color="outline-success",
                        size="sm",
                        className="me-2",
                        disabled=True,
                        title="Not implemented",
                    ),
                    dbc.Button(
                        [
                            html.I(className="fas fa-times me-2"),
                            "Close"
                        ],
                        id="close-tool-outputs-modal-btn",
                        color="secondary",
                        size="sm"
                    )
                ]
            )
        ],
        id="tool-outputs-modal",
        is_open=False,
        size="xl",
        backdrop=True,
        scrollable=True,
        className="tool-outputs-modal",
        style={"z-index": "9999"}
    )
    
    return modal


def create_show_tool_outputs_button(report_type, size="sm", className=""):
    """Create a 'Tool Outputs' button for a specific report type"""
    
    button = dbc.Button(
        [
            html.I(className="fas fa-tools me-1"),
            "Tool Outputs"
        ],
        id={"type": "show-tool-outputs-btn", "report": report_type},
        color="outline-warning",
        size=size,
        className=f"show-tool-outputs-btn {className}",
        title=f"View tool calls and outputs for {report_type.replace('_', ' ').title()}",
        style={
            "opacity": "0.8",
            "font-size": "0.75rem",
            "padding": "0.25rem 0.5rem"
        }
    )
    
    return button


def format_tool_outputs_content(tool_calls_log, report_type=None):
    """Format tool calls log into readable content for the modal"""
    if not tool_calls_log:
        no_calls_msg = f"No tool calls recorded for {report_type.replace('_', ' ').title()}." if report_type else "No tool calls recorded yet."
        return no_calls_msg
    
    content_parts = []
    
    for i, tool_call in enumerate(tool_calls_log, 1):
        # All calls should now be in dict format thanks to get_tool_calls_for_display()
        timestamp = tool_call.get('timestamp', 'Unknown')
        tool_name = tool_call.get('tool_name', 'Unknown')
        inputs = tool_call.get('inputs', {})
        output = tool_call.get('output', 'No output')
        execution_time = tool_call.get('execution_time', 'Unknown')
        status = tool_call.get('status', 'unknown')
        agent_type = tool_call.get('agent_type', 'Unknown Agent')
        symbol = tool_call.get('symbol', 'Unknown Symbol')
        
        # Status icon and color
        if status == "success":
            status_icon = "✅"
            status_color = "🟢"
        elif status == "error":
            status_icon = "❌"
            status_color = "🔴"
        else:
            status_icon = "⚪"
            status_color = "🟡"
        
        # Format inputs nicely
        inputs_json = json.dumps(inputs, indent=2, ensure_ascii=False)
        
        # Truncate very long outputs for readability
        display_output = output
        # if isinstance(output, str) and len(output) > 15000:
        #     display_output = output[:14997] + "..."
        
        tool_section = f"""## {status_icon} Tool Call #{i}: {tool_name}

**🤖 Agent:** {agent_type}  
**📈 Symbol:** {symbol}  
**⏰ Timestamp:** {timestamp}  
**⚡ Execution Time:** {execution_time}  
**📊 Status:** {status_color} {status.title()}  

**📥 Inputs:**
```json
{inputs_json}
```

**📤 Output:**
```
{display_output}
```

---
"""
        
        content_parts.append(tool_section)
    
    # Add summary at the top
    success_count = len([call for call in tool_calls_log if call.get('status') == 'success'])
    error_count = len([call for call in tool_calls_log if call.get('status') == 'error'])
    
    report_title = report_type.replace('_', ' ').title() if report_type else "All Reports"
    
    # Get symbol from first tool call if available
    symbol_info = ""
    if tool_calls_log:
        symbol = tool_calls_log[0].get('symbol')
        if symbol:
            symbol_info = f" - {symbol}"
    
    summary = f"""# 🔧 Tool Outputs - {report_title}{symbol_info}

**Total Calls:** {len(tool_calls_log)}  
**✅ Successful:** {success_count}  
**❌ Failed:** {error_count}  

---

"""
    
    return summary + "\n".join(content_parts) 