export function diagnosticEvent(_name: string, _data?: unknown) {}
export function reportWebError(error: unknown, context = '') { console.error(context, error) }
export function startDiagnostics() {}
