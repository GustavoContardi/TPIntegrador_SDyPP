/**
 * Cómo se le muestran al usuario los datos crudos del sistema.
 *
 * La regla de toda la UI: el ciudadano no tiene por qué saber qué es un nonce,
 * un lease o un pool. El dato viaja con su nombre técnico; la etiqueta que se
 * ve es cosa de la pantalla, y vive acá para que todas digan lo mismo.
 */

/** Fecha ISO en algo que se lee de un vistazo ("23 sept, 14:05"). El crudo si no se puede leer. */
export function friendlyDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('es-AR', {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

/** Segundos en minutos y segundos ("5 min", "1 min 30 s"). */
export function friendlyDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—';
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  const rest = s % 60;
  return rest ? `${m} min ${rest} s` : `${m} min`;
}

/** El modo de un minero, dicho como lo diría una persona. */
export function modeLabel(mode: string | null | undefined): string {
  return ({
    'standalone': 'por su cuenta',
    'pool-coordinator': 'lidera un equipo',
    'pool-worker': 'en equipo',
  } as Record<string, string>)[mode ?? ''] ?? 'por su cuenta';
}

/**
 * Palabras que delatan un mensaje de error pensado para un programador y no
 * para quien usa la app: nombres de infraestructura, cabeceras, formatos.
 */
const TECHNICAL = /redis|rabbit|kubernetes|k8s|docker|pod\b|spki|header|cabecera|x-signature|x-timestamp|token|nonce|hash|lease|timestamp|base64|pem|ecdsa|p-256|json|http|traceback|exception|database|\bpool\b/i;

/** Mensajes del backend que están en inglés: no se muestran tal cual. */
const ENGLISH = /\b(the|is|not|failed|failure|invalid|cannot|missing|found|already|permission|expired|reserved|empty|error|identity)\b/i;

/**
 * Un error HTTP en una frase para el usuario.
 *
 * El backend a veces explica bien el problema ("Ya tenés registrado el minero
 * …") y a veces habla para el que lo programó ("Failed to publish … RabbitMQ").
 * El primer caso se muestra tal cual; el segundo se reemplaza por una frase
 * según el código de estado, que es lo único que el usuario puede usar.
 */
export function friendlyError(err: any, fallback = 'Algo salió mal. Probá de nuevo en un momento.'): string {
  const status: number | undefined = err?.status;
  const raw = err?.error?.detail ?? err?.message;
  const detail = typeof raw === 'string' ? raw.trim() : '';

  if (detail && !TECHNICAL.test(detail) && !ENGLISH.test(detail)) return detail;

  if (status === 0) return 'No hay conexión con la red. Revisá tu conexión y probá de nuevo.';
  if (status === 401) return 'No pudimos confirmar que esta acción sea tuya. Probá de nuevo.';
  if (status === 403) return 'No tenés permiso para hacer esto.';
  if (status === 404) return 'Eso ya no existe o todavía no está disponible.';
  if (status === 409) return 'Ese nombre ya está en uso. Elegí otro.';
  if (status && status >= 500) return 'Algo falló de nuestro lado. Probá de nuevo en un momento.';
  return fallback;
}
