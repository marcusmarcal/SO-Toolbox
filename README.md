# Media Analysis Tools Suite

A comprehensive suite of tools for analyzing media streams, GOP structures, and ingest workflows.

## Version: 2.0.0

### Features

#### GOP Analyzer

- 📊 Audio/Video sync analysis with PTS jitter calculations
- 🎯 GOP structure visualization
- 📁 Support for .TS file uploads and analysis
- 📋 European format time display (HH:mm:ss dd/mm/yyyy)
- 🖨️ Printable and downloadable reports
- 🔍 History search and filtering

#### Ingest Analyzer

- 📡 Stream ingest analysis
- 📁 .TS file upload support
- 📊 Real-time metrics

#### Additional Tools

- 🌐 SRT URI Builder
- 🔌 Monitor Dashboard
- 🎬 RTS Test Player
- And more...

### Quick Start

1. Open the desired tool in your browser:

   - `GOP-Analyzer.html` - Analyze GOP structures
   - `Ingest-Analyzer.html` - Analyze ingest streams
   - `index.html` - Main dashboard

2. For GOP Analyzer:

   - Enter server URL and port, or upload a .TS file
   - Click "Analyze"
   - View results with sync metrics and GOP structure
   - Print or download report

3. For Ingest Analyzer:
   - Drag and drop or select .TS files
   - Click "Analyze"
   - Review results

### System Requirements

- Modern web browser (Chrome, Firefox, Safari, Edge)
- No server-side dependencies
- Works offline for most features

### Changelog

#### v2.0.0 - Major Release

- ✨ Added AV Sync offset checking (< 15ms PASS, < 175ms WARN)
- ✨ Added Video/Audio PTS Jitter calculations
- ✨ Added .TS file upload support for both analyzers
- ✨ Improved report visual layout (responsive, no scrollbars)
- ✨ Added GOP structure visualization in reports
- ✨ Added "Clear" buttons for all forms
- ✨ Fixed time display to European format (HH:mm:ss dd/mm/yyyy UTC)
- ✨ Scheduler now suggests UTC time + 30 minutes
- 🐛 Fixed responsive layout issues
- 📊 Enhanced table formatting and styling

#### v1.0.0 - Initial Release

- Basic GOP analysis
- Stream monitoring
- Report generation

### Contributing

To contribute:

```bash
git add .
git commit -m "feat: add new feature description"
git push origin main
```

### License

MIT

### Support

For issues and feature requests, please open an issue on GitHub.
