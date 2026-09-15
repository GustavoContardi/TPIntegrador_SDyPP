/**
 * Estado del sistema frente a una ley de un área dada.
 *
 * El NCT no abre la ventana de una ley si no hay mineros que puedan minar su
 * área: la ley queda encolada y su ventana se abre sola cuando aparezcan. Sin
 * este dato, desde la UI eso se ve igual que un sistema colgado.
 */
export interface SystemAvailability {
  available: boolean;
  category: string;
  /**
   * Acción evaluada, si se evaluó una.
   *
   * La misma área puede estar disponible para promulgar e indisponible para
   * derogar: un equipo puede votar el área y aun así vetar toda derogación.
   */
  action?: string;
  /** Mineros vivos en toda la red (latido de los últimos 15 s). */
  live_workers: number;
  /** Los que aportarían cómputo a **esta** área; los demás no la minan. */
  eligible_workers: number;
  required_workers: number;
  /** Leyes esperando en la cola. */
  queued_laws: number;
  /** Desde cuándo el sistema está sin quórum; vacío si está operativo. */
  since?: string | null;
  /** Mensaje ya redactado por el backend, para mostrar tal cual. */
  message: string;
}
