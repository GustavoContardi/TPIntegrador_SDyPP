import { ApplicationConfig } from '@angular/core';
import { provideRouter } from '@angular/router';
import { routes } from './app.routes';
import { provideAnimationsAsync } from '@angular/platform-browser/animations/async';
import { provideHttpClient } from '@angular/common/http';

// `MAT_FORM_FIELD_DEFAULT_OPTIONS` se fue con el rediseño: ya no queda ningún
// `mat-form-field` en la app. Los campos son `<input class="input">` nativos con
// las clases de Nocturne, y de Material sobrevive sólo el snackbar.
//
// Las animaciones siguen provistas porque el snackbar las necesita para entrar
// y salir; sin ellas aparece y desaparece de golpe.
export const appConfig: ApplicationConfig = {
  providers: [
    provideRouter(routes),
    provideAnimationsAsync(),
    provideHttpClient(),
  ]
};
