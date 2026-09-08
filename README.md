# poezio-plugins

## Notifications for i3blocks
Tracks which Profanity conversations have unread messages and shares that state with an i3blocks 
status-bar blocklet, so the bar can show an unread-message icon.

### Dependencies

* poezio-notifications i3blocklet

### Installation

`cp i3blocks_unread.py ~/.local/share/poezio/plugins/`

In Poezio:  
`/load i3blocks_unread`

## Signal client
Turns Poezio into a client for Signal, by talking to a locally-running signal-cli 
daemon over its JSON-RPC Unix socket. Each Signal contact/group gets its own Poezio 
window (Signal +1555...), and incoming photos are rendered as chafa-generated ASCII art previews.

### Dependencies

* signal-cli (with the device registered as a secondary device in the Signal app)
* chafa (for the images)

### Installation

In Poezio:  
`/load signal_bridge`

