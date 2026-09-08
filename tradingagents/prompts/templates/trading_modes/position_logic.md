Current Position: {current_position}

Swing Trading Position Transition Logic:
- If Current Position: LONG
  - Signal: LONG -> Hold swing position, monitor daily with multi-day horizon
  - Signal: NEUTRAL -> Request a full close of the LONG position, subject to the existing execution safety checks
  - Signal: SHORT -> Close LONG position and enter SHORT swing position

- If Current Position: SHORT
  - Signal: SHORT -> Hold swing position, monitor daily with multi-day horizon
  - Signal: NEUTRAL -> Request a full close of the SHORT position, subject to the existing execution safety checks
  - Signal: LONG -> Close SHORT position and enter LONG swing position

- If Current Position: NEUTRAL (no open position)
  - Signal: LONG -> Enter LONG swing position based on multi-timeframe setup
  - Signal: SHORT -> Enter SHORT swing position based on multi-timeframe setup
  - Signal: NEUTRAL -> Stay in cash, wait for clear swing setup with confluence

Protected reversal boundary: when an existing protective order is in place, a completed and verified exit of the current position must happen first, and a fresh decision is required before entering the opposite direction. A single decision cannot atomically reverse the position.
