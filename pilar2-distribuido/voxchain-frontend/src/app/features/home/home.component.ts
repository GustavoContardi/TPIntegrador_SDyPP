import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { RouterModule } from '@angular/router';
import { IdentityService } from '../../core/services/identity.service';

/**
 * Portada del proyecto.
 *
 * Es la primera pantalla y para mucha gente la única: tiene que explicar en
 * treinta segundos qué es esto y dejar dos caminos claros (proponer una ley o
 * poner a minar una máquina). El resto de la app queda a un clic desde la barra.
 */
@Component({
  selector: 'app-home',
  standalone: true,
  imports: [CommonModule, MatButtonModule, RouterModule],
  template: `
    <div class="home">
      <section class="hero">
        <p class="eyebrow">Blockchain de gobierno · UNLu · Sistemas Distribuidos y Programación Paralela</p>
        <h1>
          <span class="vox">VOXCHAIN</span><span class="reborn">REBORN</span>
        </h1>
        <p class="tagline">El consenso no se cuenta. Se calcula.</p>
        <p class="lead">
          Una blockchain que no mueve dinero: promulga y deroga <strong>leyes</strong>.
          Cualquiera con un par de claves propone; la voluntad colectiva se mide en
          hashes calculados, no en cabezas contadas.
        </p>

        <div class="cta">
          <a mat-raised-button color="accent" class="cta-btn" routerLink="/propose">
            Proponer leyes
          </a>
          <a mat-stroked-button class="cta-btn cta-ghost" routerLink="/workers">
            Mineros
          </a>
        </div>
        <p class="cta-hint" *ngIf="!identityService.identity()">
          No hace falta registrarse en ningún lado: tu identidad es un par de claves
          que se genera en tu navegador y la privada nunca sale de ahí.
        </p>
      </section>

      <section class="steps">
        <div class="step">
          <span class="num">1</span>
          <h3>Alguien propone</h3>
          <p>
            Proponer no cuesta trabajo de cómputo, cuesta <em>turno</em>: después de
            proponer entrás en cooldown y no podés volver a hacerlo por unas cuantas
            ventanas.
          </p>
        </div>
        <div class="step">
          <span class="num">2</span>
          <h3>La red entera mina esa ley</h3>
          <p>
            Se abre una sola ventana de votación a la vez y toda la red apunta su
            cómputo al mismo desafío. Apoyar una ley es gastar electricidad en ella.
          </p>
        </div>
        <div class="step">
          <span class="num">3</span>
          <h3>El primero que la resuelve la sella</h3>
          <p>
            El nonce ganador queda en el bloque, encadenado al anterior. Si nadie lo
            encuentra antes del cierre, la ley se descarta: el silencio también decide.
          </p>
        </div>
      </section>

      <section class="pillars">
        <article>
          <h4>Derogar cuesta más que promulgar</h4>
          <p>
            Promulgar exige <code>n</code> ceros; derogar, <code>n+1</code>. Cada cero
            multiplica el trabajo por 16. La asimetría es deliberada: deshacer lo hecho
            tiene que costarle más a la red que hacerlo.
          </p>
        </article>
        <article>
          <h4>Los pools son facciones políticas</h4>
          <p>
            Podés minar solo o fundar un equipo que reparta el espacio de búsqueda
            entre sus mineros. Quien junta más cómputo decide más leyes — el sistema
            reproduce a propósito la concentración de poder de las blockchains reales.
          </p>
        </article>
        <article>
          <h4>El coordinador no arbitra contenido</h4>
          <p>
            El NCT abre y cierra ventanas, verifica nonces y sella bloques. No opina
            sobre las leyes. Y si se cae, otro nodo toma el relevo tomando un lease
            atómico: nadie es dueño del proceso.
          </p>
        </article>
      </section>

      <section class="closing">
        <h2>Empezá por donde quieras</h2>
        <p>
          Escribí una ley y dejá que la red decida si vale el esfuerzo, o poné una
          máquina a minar y sumate a un equipo.
        </p>
        <div class="cta">
          <a mat-raised-button color="accent" class="cta-btn" routerLink="/propose">
            Proponer leyes
          </a>
          <a mat-stroked-button class="cta-btn cta-ghost" routerLink="/workers">
            Mineros
          </a>
        </div>
      </section>
    </div>
  `,
  styles: [`
    .home { max-width: 1100px; margin: 0 auto; padding: 24px 20px 80px; color: #e0e0e0; }

    .hero { text-align: center; padding: 56px 0 64px; }
    .eyebrow {
      color: #777; font-size: 0.75rem; letter-spacing: 0.14em; text-transform: uppercase;
      margin: 0 0 28px;
    }
    h1 { margin: 0; font-size: clamp(2.6rem, 8vw, 5rem); line-height: 1; letter-spacing: -0.02em; }
    .vox { font-weight: 700; color: #fff; }
    .reborn {
      font-weight: 300; color: #ffb74d; margin-left: 0.35em;
      text-shadow: 0 0 32px rgba(255, 152, 0, 0.35);
    }
    .tagline {
      margin: 20px 0 0; font-size: clamp(1.05rem, 2.6vw, 1.4rem); color: #90caf9;
      font-weight: 300; letter-spacing: 0.01em;
    }
    .lead {
      margin: 24px auto 0; max-width: 62ch; color: #b0b0b0; line-height: 1.7;
      font-size: 1.02rem;
    }
    .lead strong { color: #e0e0e0; }

    .cta { display: flex; gap: 16px; justify-content: center; flex-wrap: wrap; margin-top: 40px; }
    .cta-btn {
      min-width: 210px; height: 52px; font-size: 1rem; letter-spacing: 0.01em;
      border-radius: 26px !important;
    }
    .cta-ghost { color: #e0e0e0 !important; border-color: #4a4a4a !important; }
    .cta-hint { margin: 24px auto 0; max-width: 56ch; color: #777; font-size: 0.85rem; line-height: 1.6; }

    .steps {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 24px;
      padding: 48px 0; border-top: 1px solid #262626; border-bottom: 1px solid #262626;
    }
    .step .num {
      display: inline-flex; align-items: center; justify-content: center;
      width: 32px; height: 32px; border-radius: 50%;
      background: rgba(255, 152, 0, 0.12); color: #ffb74d;
      border: 1px solid rgba(255, 152, 0, 0.3);
      font-size: 0.85rem; font-weight: 700;
    }
    .step h3 { margin: 16px 0 8px; color: #e0e0e0; font-size: 1.05rem; font-weight: 500; }
    .step p { margin: 0; color: #999; line-height: 1.65; font-size: 0.92rem; }
    .step em { color: #b0b0b0; font-style: normal; border-bottom: 1px dotted #555; }

    .pillars {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px;
      padding: 48px 0;
    }
    .pillars article {
      background: #1a1a1a; border: 1px solid #2c2c2c; border-radius: 10px; padding: 24px;
    }
    .pillars h4 { margin: 0 0 12px; color: #e0e0e0; font-size: 1rem; font-weight: 600; line-height: 1.4; }
    .pillars p { margin: 0; color: #999; line-height: 1.65; font-size: 0.9rem; }
    .pillars code {
      font-family: 'Courier New', monospace; color: #90caf9; background: #0c0c0c;
      padding: 2px 6px; border-radius: 4px; border: 1px solid #222;
    }

    .closing { text-align: center; padding: 40px 0 0; border-top: 1px solid #262626; }
    .closing h2 { margin: 0 0 12px; color: #e0e0e0; font-weight: 400; font-size: 1.6rem; }
    .closing p { margin: 0 auto; max-width: 54ch; color: #999; line-height: 1.65; }
  `]
})
export class HomeComponent {
  identityService = inject(IdentityService);
}
