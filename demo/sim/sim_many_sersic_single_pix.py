import sys, os
from functools import partial as argfix
import numpy as np
import matplotlib.pyplot as pl

from forcepho import paths
from forcepho.sources import Galaxy, Scene
from forcepho.likelihood import WorkPlan, make_image, lnlike_multi

from demo_utils import make_real_stamp as make_stamp
from demo_utils import Posterior, Result
    

psfpaths = {"F090W": os.path.join(paths.psfmixture, 'f090_ng6_em_random.p'),
            "F115W": os.path.join(paths.psfmixture, 'f090_ng6_em_random.p'),
            "F150W": os.path.join(paths.psfmixture, 'f150w_ng6_em_random.p'),
            "F200W": os.path.join(paths.psfmixture, 'f200w_ng6_em_random.p'),
            "F277W": os.path.join(paths.psfmixture, 'f200w_ng6_em_random.p'),
            }


def prep_stamps(imnames, psfnames, ra_center, dec_center, size=(50, 50)):
    # HUUGE HAAAACK
    if filters[0] == "F277W":
        psfcenter = (496/2. - 100)
        psf_realization = 0
    else:
        psfcenter = 104.
        psf_realization = 2

    # --- Build the postage stamp ----
    pos = (ra_center, dec_center)
    stamps = [make_stamp(im, pos, center_type='celestial', size=size,
                         psfname=pn, psfcenter=psfcenter, fix_header=True,
                         psf_realization=psf_realization)
              for im, pn in zip(imnames, psfnames)]

    # Background subtract.  yuck
    for stamp in stamps:
        bkg = np.nanmedian(stamp.pixel_values[:5, :])  # stamp.full_header["BKG"]
        stamp.pixel_values -= bkg # 
        stamp.subtracted_background = bkg
        
    # override the psf to reflect in both directions
    T = -1.0 * np.eye(2)
    for s in stamps:
        s.psf.covariances = np.matmul(T, np.matmul(s.psf.covariances, T.T))
        s.psf.means = np.matmul(s.psf.means, T)

    return stamps


def prep_scene(sourcepars, filters=["dummy"], splinedata=None, free_sersic=True):

    # --- Get Sources and a Scene -----
    sources = []

    for pars in sourcepars:
        flux, x, y, q, pa, n, rh = np.copy(pars)
        s = Galaxy(filters=filters, splinedata=splinedata, free_sersic=free_sersic)
        s.sersic = n
        s.rh = rh
        s.flux = flux
        s.ra = x
        s.dec = y
        s.q = q
        s.pa = np.deg2rad(pa)
        sources.append(s)

    scene = Scene(sources)
    theta = scene.get_all_source_params()
    return scene, theta


def read_cat(catname):
    cols = ['id', 'ra', 'dec', 'a', 'b', 'pa', 'n', 'mag']
    usecols = [0, 1, 2, 5, 6, 7, 8, 9]
    dt = np.dtype([(c, np.float) for c in cols])
    cat = np.genfromtxt(catname, usecols=usecols, dtype=dt)
    return cat


def cat_to_sourcepars(catrow):
    ra, dec = catrow["ra"], catrow["dec"]
    q = np.sqrt(catrow["b"] / catrow["a"])
    pa = 90.0 - catrow["pa"]
    n = catrow["n"]
    S = np.array([[1/q, 0], [0, q]])
    rh = np.mean(np.dot(np.linalg.inv(S), np.array([catrow["a"], catrow["b"]])))
    return [ra, dec, q, pa, n, rh]
            


if __name__ == "__main__":


    filters = ["F090W"]
    sca = ["482"]
    exps = ["001"]
    cutout_size = (40, 40)

    # ------------------------------------
    # --- Choose a scene center and get some source guesses---
    catnames = [os.path.join(paths.galsims, '031718', "tri_gal_cat_{}.txt".format(s)) for s in sca]
    cat = read_cat(catnames[0])

    sceneid = 7
    gal = cat[cat["mag"] == 25.0][3 * sceneid]
    choose = (np.abs(cat["ra"] - gal["ra"]) < 1e-4) & (np.abs(cat["dec"] - gal["dec"]) < 2e-4)
    catscene = cat[choose]
    
    ra_ctr, dec_ctr = catscene["ra"].mean(), catscene["dec"].mean()

    # HACK!!!
    zp = 27.4525
    mag_offset = 0.4
    fluxes = [[10**(0.4 * (zp - s["mag"] - mag_offset))] for s in catscene]
    sourcepars = [tuple([flux] + cat_to_sourcepars(s)) for flux, s in zip(fluxes, catscene)]

    # --------------------------------
    # --- Setup Scene and Stamp(s) ---
    imnames = ['sim_cube_{}_{}_{}.slp.fits'.format(f, s, e) for f, s, e in zip(filters, sca, exps)]
    imnames = [os.path.join(paths.galsims, '031718', im) for im in imnames]
    psfnames = [psfpaths[f] for f in filters]

    stamps = prep_stamps(imnames, psfnames, ra_ctr, dec_ctr, size=cutout_size)

    # HACK! Do fitting in pixel space
    # x,y coordinates
    stamp = stamps[0]
    sourcepars = [list(sp) for sp in sourcepars]
    for sp in sourcepars:
        xy = stamp.sky_to_pix(np.array(sp[1:3]))
        sp[1:3] = [xy[0], xy[1]]
    sourcepars = [tuple(sp) for sp in sourcepars]
        
    # Now remove stamp astrometry (but keep stamp.scale)
    stamp.dpix_dsky = np.eye(2)
    stamp.crval = np.zeros([2])
    stamp.crpix = np.zeros([2])

    plans = [WorkPlan(stamp) for stamp in stamps]
    scene, theta = prep_scene(sourcepars, filters=np.unique(filters).tolist(),
                              splinedata=paths.galmixture)

    theta_init = theta.copy()
    ptrue = theta.copy()
    p0 = ptrue.copy()
    ndim = len(theta)
    nsource = len(sourcepars)

    initial, _ = make_image(scene, stamps[0], Theta=theta_init)
    #sys.exit()

    # --------------------------------
    # --- Show model and data ---
    if True:
        from phoplot import plot_model_images
        fig, axes = plot_model_images(ptrue, scene, stamps)
        pl.show()

    # --------------------------------
    # --- Priors ---
    plate_scale = np.linalg.eigvals(np.linalg.inv(stamps[0].dpix_dsky))
    plate_scale = np.abs(plate_scale).mean()

    upper = [[12.0, s[1] + 3 * plate_scale, s[2] + 3 * plate_scale, 1.0, np.pi/2, 5.0, 0.12]
             for s in sourcepars]
    lower = [[0.0, s[1] - 3 * plate_scale, s[2] - 3 * plate_scale, 0.3, -np.pi/2, 1.2, 0.015]
             for s in sourcepars]
    upper = np.concatenate(upper)
    lower = np.concatenate(lower)

    # --------------------------------
    # --- sampling ---
    import time

    # --- hemcee ---
    if True:
        p0 = ptrue.copy()
        scales = upper - lower
        scales = np.array(nsource * [ 1., plate_scale, plate_scale, 0.1, 0.1, 0.1, 0.01])
        #scales = np.array([ 50. ,   10. ,   10. ,   1.,   3. ,   4. ,   0.1 ])
        #scales = np.array([100., 5., 5., 1., 3., 5., 1.0])
        
        from hemcee import NoUTurnSampler
        from hemcee.metric import DiagonalMetric
        metric = DiagonalMetric(scales**2)
        model = Posterior(scene, plans, upper=np.inf, lower=-np.inf)
        sampler = NoUTurnSampler(model.lnprob, model.lnprob_grad, metric=metric)

        t = time.time()
        pos, lnp0 = sampler.run_warmup(p0, 500)
        twarm = time.time() - t
        nwarm = np.copy(model.ncall)
        model.ncall = 0
        sys.exit()
        t = time.time()
        chain, lnp = sampler.run_mcmc(pos, 2000)
        tsample = time.time() - t
        nsample = np.copy(model.ncall)
        best = chain[lnp.argmax(), :]

        import cPickle as pickle
        result = Result()
        result.ndim = len(p0)
        result.chain = chain
        result.lnp = lnp
        result.ncall = nsample
        result.wall_time = tsample
        result.sourcepars = sourcepars
        result.stamps = stamps
        result.filters = filters
        result.plans = plans
        result.scene = scene
        result.truths = ptrue.copy()
        result.metric = np.copy(metric.variance)
        result.step_size = sampler.step_size.get_step_size()
        with open("sim_sersic_single_hemcee.pkl", "wb") as f:
            pickle.dump(result, f)

        fig, axes = pl.subplots(7, 3, sharex=True)
        for i, ax in enumerate(axes.T.flat): ax.plot(chain[:, i])
        for i, ax in enumerate(axes.T.flat): ax.axhline(p0[i], color='k', linestyle=':')
        for i, ax in enumerate(axes.T.flat): ax.set_title(label[i])


    # --- nested ---
    if False:
        lnlike = argfix(lnlike_multi, scene=scene, plans=plans, grad=False)
        theta_width = (upper - lower)
        nlive = 50
        
        def prior_transform(unit_coords):
            # now scale and shift
            theta = lower + theta_width * unit_coords
            return theta

        import dynesty
        
        # "Standard" nested sampling.
        sampler = dynesty.DynamicNestedSampler(lnlike, prior_transform, ndim, nlive=nlive,
                                               bound="multi", method="slice", bootstrap=0)
        t0 = time.time()
        sampler.run_nested(nlive_init=int(nlive/2), nlive_batch=int(nlive),
                           wt_kwargs={'pfrac': 1.0}, stop_kwargs={"post_thresh":0.2})
        dur = time.time() - t0
        results = sampler.results
        results['duration'] = dur
        indmax = results['logl'].argmax()
        best = results['samples'][indmax, :]

        from dynesty import plotting as dyplot
        truths = ptrue.copy()
        label = filters + ["ra", "dec", "q", "pa", "n", "rh"]
        cfig, caxes = dyplot.cornerplot(results, fig=pl.subplots(ndim, ndim, figsize=(13., 10)),
                                        labels=label, show_titles=True, title_fmt='.8f', truths=truths)
        tfig, taxes = dyplot.traceplot(results, fig=pl.subplots(ndim, 2, figsize=(13., 13.)),
                                    labels=label)

    # -- hmc ---
    if False:
        p0 = ptrue.copy()
        prange = upper - lower
        scales = np.array(nsource * [ 5., plate_scale, plate_scale, 1.0, 1. ])

        from hmc import BasicHMC
        model = Posterior(scene, plans, upper=upper, lower=lower)
        sampler = BasicHMC(model, verbose=False)
        sampler.ndim = len(p0)


        sampler.set_mass_matrix(1/scales**2)
        eps = sampler.find_reasonable_stepsize(p0*1.0)
        use_eps = eps / 2.0 #1e-2
        print(eps)
        result.step_size = np.copy(use_eps)
        result.metric = scales**2
        #sys.exit()
        
        pos, prob, grad = sampler.sample(p0 + 0.2 * prange, iterations=500, mass_matrix=1/scales**2,
                                         epsilon=use_eps, length=20, sigma_length=5,
                                         store_trajectories=True)
        #eps = sampler.find_reasonable_stepsize(pos)
        #pos, prob, grad = sampler.sample(pos, iterations=100, mass_matrix=1/scales**2,
        #                                 epsilon=use_eps, length=30, sigma_length=8,
        #                                 store_trajectories=True)

        best = sampler.chain[sampler.lnp.argmax()]

        result = Result()
        result.ndim = len(p0)
        result.sourcepars = sourcepars
        result.stamps = stamps
        result.filters = filters
        result.offsets = None
        result.plans = plans
        result.scene = scene
        result.truths = ptrue.copy()
        result.upper = upper
        result.lower = lower
        result.chain = sampler.chain.copy()
        result.lnp = sampler.lnp.copy()
        result.trajectories = sampler.trajectories
        result.accepted = sampler.accepted
        import cPickle
        with open("sim_sersic_single_hmc.pkl", "wb") as f:
            pickle.dump(result, f)

        from phoplot import plot_chain
        out = plot_chain(sampler, show_trajectories=True, equal_axes=True, source=2)
        #import corner
        #cfig = corner.corner(sampler.chain[10:], truths=ptrue.copy(), labels=label, show_titles=True)
        #sys.exit()
        
    # ---------------------------
    # --- Plot results ---
    if True:
        # plot the data and model
        from phoplot import plot_model_images
        rfig, raxes = plot_model_images(best, scene, stamps)
        pl.show()
