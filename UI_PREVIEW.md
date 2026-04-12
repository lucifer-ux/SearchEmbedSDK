# UI Preview - ContextCore Init Welcome Screen

## What Users Will See

When running `contextcore init`, users will be greeted with this Textual-based interface:

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                            ContextCore CLI                              ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
                                                                            
                ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓               
                ┃ Welcome to ContextCore                ┃               
                ┃                                       ┃               
                ┃ Your intelligent document search      ┃               
                ┃ and indexing system.                  ┃               
                ┃ ContextCore helps you search across   ┃               
                ┃ your files using AI-powered           ┃               
                ┃ embeddings.                           ┃               
                ┃                                       ┃               
                ┃ Select your preferred theme:          ┃               
                ┃                                       ┃               
                ┃ ◉ Dark Mode (recommended)             ┃               
                ┃ ○ Light Mode                          ┃               
                ┃                                       ┃               
                ┃ [Press ENTER to confirm, Q to quit]   ┃               
                ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛               
```

## UI Components

### 1. Header (Textual Header Widget)
- Professional terminal header displaying "ContextCore CLI"
- No clock display for cleaner appearance

### 2. Welcome Message Box (WelcomeMessage Component)
- **Title**: "Welcome to ContextCore" in bold cyan
- **Description**: Information about what ContextCore does in dimmed text
- **Styling**: Heavy border around message, centered layout

### 3. Theme Selection (ThemeSelector Component)
- **Label**: "Select your preferred theme:" in bold
- **Options**:
  - ◉ Dark Mode (recommended) - **Default selected**
  - ○ Light Mode
- **Type**: RadioSet with RadioButtons (mutually exclusive)
- **Styling**: Centered, with proper spacing and margins

### 4. Instructions
- Clear keyboard shortcuts displayed
- `Enter` to confirm selection
- `Q` to quit (defaults to dark mode)

## Layout Properties

| Property | Value | Purpose |
|----------|-------|---------|
| **Alignment** | Center Middle | All content centered both horizontally and vertically |
| **Border** | Heavy | Visual separation of content |
| **Padding** | 2 units | Breathing room inside the container |
| **Margin** | 1 unit | Space around the outer container |
| **Width** | Full width (1fr) | Responsive to terminal size |
| **Height** | Full height (1fr) | Fills available vertical space |

## User Interaction Flow

```
User runs: contextcore init
    ↓
Textual UI initializes and displays
    ↓
User sees welcome message and theme options
    ↓
User selects theme using arrow keys or mouse
    ↓
User presses Enter to confirm
    ↓
Selection returned to run_init()
    ↓
_choose_setup_theme() sets up the questionary style
    ↓
Regular init wizard continues with theme applied
```

## Features

### Centered Layout
- ✓ Content is centered on the screen
- ✓ Works with any terminal size
- ✓ Professional appearance

### Theme Selection
- ✓ Only one option can be selected at a time (RadioButton behavior)
- ✓ Dark mode is pre-selected as recommended
- ✓ Clear visual indication of selection (◉ vs ○)

### User-Friendly
- ✓ Clear welcome message explains what ContextCore does
- ✓ Simple and intuitive option selection
- ✓ Clear instructions for navigation
- ✓ Keyboard shortcuts shown

### Robust
- ✓ Graceful fallback to questionary if Textual fails
- ✓ Exception handling for edge cases
- ✓ Always returns a valid theme ("dark" or "light")

## CSS Styling Details

The UI uses Textual CSS (TCSS) for styling:

```css
ContextCoreInitApp {
    layout: vertical;
    background: $surface;
}

#main-container {
    width: 1fr;
    height: 1fr;
    align: center middle;
    border: heavy $primary;
    margin: 1;
    padding: 2;
    background: $panel;
}

#content {
    width: auto;
    height: auto;
    align: center middle;
}

#theme-section {
    width: auto;
    height: auto;
    margin: 2 0;
}

WelcomeMessage {
    margin: 1 0;
    width: 1fr;
    border: heavy $primary;
    padding: 1 2;
    background: $surface;
}

ThemeSelector {
    width: 1fr;
    height: auto;
    margin: 1 0;
}
```

## Keyboard Controls

| Key | Action |
|-----|--------|
| ↑ / ↓ | Navigate between radio button options |
| Space | Toggle selection (alternative to mouse click) |
| Enter | Confirm selection and proceed |
| Q | Quit without saving (defaults to dark mode) |
| Tab | Focus navigation |

## Example Terminal Sizes

The UI adapts to different terminal sizes while maintaining centered layout:

### Wide Terminal (120 columns, 40 rows)
- Extra horizontal space used efficiently
- Content remains centered

### Standard Terminal (80 columns, 24 rows)
- Content still readable and centered
- Proper text wrapping in welcome message

### Narrow Terminal (60 columns, 20 rows)
- Text may wrap but remains usable
- All controls remain accessible
