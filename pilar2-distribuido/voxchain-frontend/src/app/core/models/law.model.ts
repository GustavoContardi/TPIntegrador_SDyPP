import { SystemAvailability } from './system.model';

export interface Law {
  law_id: string;
  author_pubkey: string;
  text_hash: string;
  text_ref?: string;
  text_compressed?: string;
  text_original_len?: number;
  status: string;
  action: string;
  /** Área de gobierno de la ley: decide qué equipos aportan cómputo a su ventana. */
  category: string;
  created_at: string;
}

export interface LawProposalRequest {
  law_id?: string;
  author_pubkey: string;
  text: string;
  action: string;
  category?: string;
  // Campos firmados por el cliente (A-01). El backend verifica la firma.
  text_hash?: string;
  created_at?: string;
  signature?: string;
}

/**
 * Respuesta del POST de una propuesta: la ley más el estado del sistema.
 *
 * `availability` viene sólo en el alta. Si `available` es false la ley se
 * aceptó igual —está encolada— pero su ventana no se abre todavía, y hay que
 * decírselo al autor en ese momento en vez de mostrarle un éxito liso.
 */
export interface LawProposalResponse extends Law {
  availability?: SystemAvailability | null;
}

/**
 * Si una identidad puede proponer leyes. Sólo pueden el fundador de un equipo
 * y el dueño de un minero standalone; `reason` explica por qué no, listo para
 * mostrar. Es un aviso: la autorización la hace el POST.
 */
export interface ProposerStanding {
  allowed: boolean;
  role: 'team_owner' | 'standalone' | null;
  reason: string;
}

export interface LawCategory {
  value: string;
  label: string;
}

/**
 * Categorías de ley conocidas por el cliente.
 *
 * La lista autoritativa la sirve `GET /api/laws/categories` — es la misma que
 * valida el NCT — y ésta es sólo el respaldo para pintar la UI antes de que
 * llegue la respuesta, o si el backend no está disponible. Si las dos difieren,
 * gana la del backend: proponer con un slug que él no conoce da 400.
 */
export const LAW_CATEGORIES: LawCategory[] = [
  { value: 'economia', label: 'Economía y finanzas' },
  { value: 'salud', label: 'Salud' },
  { value: 'educacion', label: 'Educación' },
  { value: 'seguridad', label: 'Seguridad y justicia' },
  { value: 'ambiente', label: 'Ambiente' },
  { value: 'infraestructura', label: 'Infraestructura y transporte' },
  { value: 'derechos', label: 'Derechos y ciudadanía' },
  { value: 'general', label: 'General' },
];

export const DEFAULT_CATEGORY = 'general';

/** Etiqueta legible de una categoría; el slug crudo si no la conocemos. */
export function categoryLabel(category: string | undefined | null,
                              known: LawCategory[] = LAW_CATEGORIES): string {
  if (!category) return categoryLabel(DEFAULT_CATEGORY, known);
  return known.find((c) => c.value === category)?.label ?? category;
}

/**
 * La acción en castellano; el slug crudo si es una que no conocemos.
 *
 * El backend guarda `promulgacion` / `derogacion` sin tilde —son identificadores
 * y viajan en la firma, así que no se tocan— pero mostrarlos tal cual deja
 * "promulgacion" a la vista en media app. Existe por la misma razón que
 * `categoryLabel`: el dato es el slug, la etiqueta es cosa de la UI.
 */
export function actionLabel(action: string | undefined | null): string {
  if (!action) return '';
  return ({
    promulgacion: 'promulgación',
    derogacion: 'derogación',
  } as Record<string, string>)[action] ?? action;
}

/**
 * El estado de una ley en castellano; el crudo del backend si no lo conocemos.
 *
 * "En ventana" era la jerga interna: para quien mira, una ley en ventana es
 * una ley que se está votando.
 */
export function lawStatusLabel(status: string | undefined | null): string {
  if (!status) return '';
  return ({
    pending_queue: 'esperando turno',
    in_deliberation: 'por votarse',
    in_window: 'en votación',
    promulgated: 'vigente',
    repealed: 'derogada',
    discarded: 'descartada',
  } as Record<string, string>)[status] ?? status;
}
