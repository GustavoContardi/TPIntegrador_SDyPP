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
