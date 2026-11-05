// The editor core and its contributions (folding, sticky scroll, find, ...)
// without Monaco's language packs: gitr-serve tokenizes its diff itself.
import * as monaco from 'monaco-editor/editor/editor.api.js';
import 'monaco-editor/features/register.all.js';
window.monaco = monaco;
