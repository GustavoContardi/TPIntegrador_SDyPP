/** Respuesta de un convocado: aporta cómputo o no. Sin respuesta, no mina. */
export type DeliberationDecision = 'accept' | 'reject';

/** Un convocado a decidir: un equipo entero (responde su fundador) o un standalone. */
export interface DeliberationVoter {
  voter_id: string;
  kind: 'equipo' | 'standalone';
  name: string;
  owner: string;
  team_id: string;
  hashrate: number;
  decision: DeliberationDecision | null;
  /** Lo que cuenta si no responde: la respuesta por defecto, o '' (no mina). */
  default_decision: DeliberationDecision | '';
  /** El más grande del área: sobre él se congeló la dificultad. */
  biggest: boolean;
}

/**
 * La ley anunciada, antes de su ventana (AGENT.md 3.12).
 *
 * Trae sólo la ley y su área: el id de ventana y el desafío recién existen al
 * abrirse, para que nadie pueda empezar a minar durante la pausa.
 */
export interface Deliberation {
  law_id: string;
  action: string;
  category: string;
  started_at: string;
  decide_until: string;
  seconds_left: number;
  n_zeros_required: number;
  window_seconds: number;
  voters: DeliberationVoter[];
}
