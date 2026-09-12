from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
from langchain_core.messages import AIMessage, ToolMessage
from tradingagents.dataflows.interface_utils import analysis_date_mode
from tradingagents.llm_clients.retry import ProviderFailure
from tradingagents.prompts import load_prompt, render_prompt

# Import prompt capture utility
try:
    from webui.utils.prompt_capture import capture_agent_prompt
except ImportError:
    # Fallback for when webui is not available
    def capture_agent_prompt(report_type, prompt_content, symbol=None):
        pass


def create_fundamentals_analyst(llm, toolkit):
    def fundamentals_analyst_node(state):
        # print(f"[FUNDAMENTALS] Starting fundamentals analysis for {state['company_of_interest']}")
        start_time = time.time()
        
        try:
            current_date = state["trade_date"]
            ticker = state["company_of_interest"]
            company_name = state["company_of_interest"]
            
            # print(f"[FUNDAMENTALS] Analyzing {ticker} on {current_date}")
            
            # Check if the ticker is a cryptocurrency
            is_crypto = "/" in ticker or "USD" in ticker.upper() or "USDT" in ticker.upper()
            # print(f"[FUNDAMENTALS] Detected asset type: {'Cryptocurrency' if is_crypto else 'Stock'}")
            
            # Extract base ticker for cryptocurrencies (BTC from BTC/USD or BTCUSDT)
            display_ticker = ticker
            if is_crypto:
                # Remove USD, USDT or anything after /
                if "/" in ticker:
                    display_ticker = ticker.split("/")[0]
                elif "USDT" in ticker.upper():
                    display_ticker = ticker.upper().replace("USDT", "")
                elif "USD" in ticker.upper():
                    display_ticker = ticker.upper().replace("USD", "")

            openai_available = toolkit.has_openai_web_search()
            finnhub_available = toolkit.has_finnhub()
            simfin_available = toolkit.has_simfin_data()
            # Live-only sources (OpenAI hosted web search, DeFiLlama) are
            # never offered on a historical as-of date; SEC/SimFin/Finnhub
            # have point-in-time capability and stay available.
            analysis_mode = analysis_date_mode(current_date)
            live_only_sources_allowed = analysis_mode == "live"

            if (
                toolkit.config["online_tools"]
                and openai_available
                and live_only_sources_allowed
            ):
                base_openai_tools = [toolkit.get_fundamentals_openai]
            else:
                base_openai_tools = []

            if is_crypto:
                tools = (
                    [toolkit.get_defillama_fundamentals]
                    if live_only_sources_allowed
                    else []
                ) + base_openai_tools
                active_sources = (
                    ["DeFiLlama"] if live_only_sources_allowed else []
                ) + (["OpenAI web search"] if base_openai_tools else [])
            else:
                tools = [toolkit.get_sec_ir_source] if toolkit.config.get("sec_ir_enabled", True) else []
                tools.extend(base_openai_tools)
                if finnhub_available:
                    tools.extend(
                        [
                            toolkit.get_finnhub_company_insider_sentiment,
                            toolkit.get_finnhub_company_insider_transactions,
                        ]
                    )
                if simfin_available:
                    tools.extend(
                        [
                            toolkit.get_simfin_balance_sheet,
                            toolkit.get_simfin_cashflow,
                            toolkit.get_simfin_income_stmt,
                        ]
                    )
                active_sources = ["SEC/IR filing metadata (not verified financial figures)"] if toolkit.config.get("sec_ir_enabled", True) else []
                if base_openai_tools:
                    active_sources.append("OpenAI web search")
                if finnhub_available:
                    active_sources.append("Finnhub")
                if simfin_available:
                    active_sources.append("SimFin")

            source_guidance = (
                " Use all available fundamentals tools before concluding. "
                + (f"Active sources now: {', '.join(active_sources)}." if active_sources else "No external fundamentals source is available; reason from existing context only.")
            )
            if analysis_mode != "live":
                source_guidance += (
                    f" Historical as-of {current_date}: live-only sources without a"
                    " verified point-in-time cutoff (OpenAI web search, DeFiLlama)"
                    " are unavailable for this date. If no active source can"
                    " evidence a claim, state it explicitly as 'source unavailable';"
                    " never substitute present-day data."
                )
            asset_focus = (
                "Analyze DeFi metrics like TVL changes, protocol upgrades, token unlock schedules, yield farming opportunities, and major partnership announcements that could sustain multi-day crypto price trends."
                if is_crypto
                else "Focus on earnings surprises, analyst upgrades/downgrades, insider activity, and fundamental shifts that could sustain multi-day swing moves."
            )
            system_message = render_prompt(
                "analysts/fundamentals_system",
                asset_focus=asset_focus,
                source_guidance=source_guidance,
            )
            asset_context = (
                f"The cryptocurrency we want to analyze is {display_ticker}"
                if is_crypto
                else f"The company we want to look at is {ticker}"
            )

            prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        load_prompt("shared/analyst_tool_system"),
                    ),
                    MessagesPlaceholder(variable_name="messages"),
                ]
            )

            # print(f"[FUNDAMENTALS] Setting up prompt and chain...")
            prompt = prompt.partial(system_message=system_message)
            prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
            prompt = prompt.partial(current_date=current_date)
            prompt = prompt.partial(ticker=display_ticker if is_crypto else ticker)
            prompt = prompt.partial(asset_context=asset_context)

            # Capture the COMPLETE resolved prompt that gets sent to the LLM
            try:
                # Get the formatted messages with all variables resolved
                messages_history = list(state["messages"])
                formatted_messages = prompt.format_messages(messages=messages_history)
                
                # Extract the complete system message (first message)
                if formatted_messages and hasattr(formatted_messages[0], 'content'):
                    complete_prompt = formatted_messages[0].content
                else:
                    # Fallback: manually construct the complete prompt
                    tool_names_str = ", ".join([tool.name for tool in tools])
                    complete_prompt = render_prompt(
                        "shared/analyst_tool_system",
                        tool_names=tool_names_str,
                        system_message=system_message,
                        current_date=current_date,
                        asset_context=asset_context,
                    )
                
                capture_agent_prompt("fundamentals_report", complete_prompt, ticker)
            except Exception as e:
                print(f"[FUNDAMENTALS] Warning: Could not capture complete prompt: {e}")
                # Fallback to system message only
                capture_agent_prompt("fundamentals_report", system_message, ticker)

            chain = prompt | (llm.bind_tools(tools) if tools else llm)
            
            # print(f"[FUNDAMENTALS] Invoking LLM chain...")
            # Copy the incoming conversation history so we can append to it when the model makes tool calls
            messages_history = list(state["messages"])

            # First LLM response
            result = chain.invoke(messages_history)
            max_tool_iterations = int(toolkit.config.get("max_tool_iterations_per_agent", 8))
            max_same_call_repeats = int(toolkit.config.get("max_same_tool_call_repeats", 1))
            tool_call_counts = {}
            tool_result_cache = {}
            iteration_count = 0

            # Handle iterative tool calls until the model stops requesting them
            while tools and getattr(result, "additional_kwargs", {}).get("tool_calls") and iteration_count < max_tool_iterations:
                iteration_count += 1
                for tool_call in result.additional_kwargs["tool_calls"]:
                    # Handle different tool call structures
                    if isinstance(tool_call, dict):
                        tool_name = tool_call.get("name") or tool_call.get("function", {}).get("name")
                        tool_args = tool_call.get("args", {}) or tool_call.get("function", {}).get("arguments", {})
                        if isinstance(tool_args, str):
                            try:
                                tool_args = json.loads(tool_args)
                            except json.JSONDecodeError:
                                tool_args = {}
                    else:
                        # Handle LangChain ToolCall objects
                        tool_name = getattr(tool_call, 'name', None)
                        tool_args = getattr(tool_call, 'args', {})

                    # Find the matching tool by name
                    tool_fn = next((t for t in tools if t.name == tool_name), None)

                    if tool_fn is None:
                        tool_result = f"Tool '{tool_name}' not found."
                        # print(f"[FUNDAMENTALS] ⚠️ {tool_result}")
                    else:
                        call_signature = f"{tool_name}:{json.dumps(tool_args, sort_keys=True, default=str)}"
                        call_count = tool_call_counts.get(call_signature, 0) + 1
                        tool_call_counts[call_signature] = call_count

                        if call_signature in tool_result_cache:
                            tool_result = tool_result_cache[call_signature]
                        elif call_count > max_same_call_repeats:
                            tool_result = (
                                f"Skipped repeated tool call '{tool_name}' with identical arguments "
                                f"after {max_same_call_repeats} execution(s). Reuse previous evidence."
                            )
                        else:
                            try:
                                # LangChain Tool objects expose `.run` (string IO) as well as `.invoke` (dict/kwarg IO)
                                if hasattr(tool_fn, "invoke"):
                                    tool_result = tool_fn.invoke(tool_args)
                                else:
                                    tool_result = tool_fn.run(**tool_args)
                                tool_result_cache[call_signature] = str(tool_result)
                            except Exception as tool_err:
                                tool_result = f"Error running tool '{tool_name}': {str(tool_err)}"

                    # Append the assistant tool call and tool result messages so the LLM can continue the conversation
                    tool_call_id = tool_call.get("id") or tool_call.get("tool_call_id")
                    ai_tool_call_msg = AIMessage(
                        content="",
                        additional_kwargs={"tool_calls": [tool_call]},
                    )
                    tool_msg = ToolMessage(
                        content=str(tool_result),
                        tool_call_id=tool_call_id,
                    )

                    messages_history.append(ai_tool_call_msg)
                    messages_history.append(tool_msg)

                # Ask the LLM to continue with the new context
                result = chain.invoke(messages_history)

            tool_loop_exhausted = bool(
                tools
                and getattr(result, "additional_kwargs", {}).get("tool_calls")
            )
            if tool_loop_exhausted:
                # H-08: the model still demanded tool calls after the last
                # iteration — there is no report. An empty content keeps the
                # analyst "failed" so the coverage gate rejects the round.
                result = AIMessage(content="")
             
            elapsed_time = time.time() - start_time
            # print(f"[FUNDAMENTALS] ✅ Analysis completed in {elapsed_time:.2f} seconds")
            # print(f"[FUNDAMENTALS] Generated report length: {len(result.content)} characters")

            # The analyst report is exactly what the tool loop produced. No
            # separate final-recommendation call exists: analysts never emit
            # executable actions, and an empty report is handled by the
            # selected-analyst coverage gate, not patched here.
            analysis_content = (result.content or "").strip()
            result = AIMessage(content=analysis_content)

            # Append final assistant response to history for downstream agents
            messages_history.append(result)

            merged_status = "completed" if analysis_content else "failed"
            return {
                "messages": messages_history,
                "fundamentals_report": result.content,
                "analysis_status": {
                    **(state.get("analysis_status") or {}),
                    "fundamentals": merged_status,
                },
                "analysis_errors": (
                    {
                        **(state.get("analysis_errors") or {}),
                        "fundamentals": "fundamentals analyst returned an empty report",
                    }
                    if merged_status == "failed"
                    else state.get("analysis_errors") or {}
                ),
            }

        except ProviderFailure:
            # Provider access failures keep their original type and stop the round.
            raise

        except Exception as e:
            elapsed_time = time.time() - start_time
            error_msg = f"Error in fundamentals analysis for {state['company_of_interest']}: {str(e)}"
            print(f"[FUNDAMENTALS] ❌ {error_msg}")
            print(f"[FUNDAMENTALS] ❌ Failed after {elapsed_time:.2f} seconds")
            # Sanitized error record, then re-raise: a broad exception must
            # never be repackaged as a completed report.
            import traceback
            import os as _os
            if _os.environ.get("TRADINGAGENTS_DEBUG_TRACEBACK"):
                traceback.print_exc()
            raise RuntimeError(error_msg) from e

    return fundamentals_analyst_node
