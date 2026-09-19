"""

YTCM.py
=======

Do not expect any groundbreaking methodology here; YTCM does not invent anything from scratch,
but tries to adapt what is already in use (see the list of references down here): it tries to integrate
downloading and analysis functions into a tool that can serve as an environment for basic analysis,
to be followed by corpus export to more specialized tools that YTCM does not even try (and would never
be able to) to imitate. Much of its design still reflects its original purpose, namely the musicological
analysis of East Asian popular music cultures, and many features have been added ad hoc around that
initial focus. Still, I believe it is useful to have many different approaches such as this one
documented online, since each models and focuses on a particular facet of YouTube and its cultural
impact on (not only) music distribution and discourse.

"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import cmd
import logging
import os

from argparse                   import ArgumentParser
from datetime                   import datetime

from ytcm.config import (API_KEY_FILE, EXCLUDED_TERMS, PRIMARY_SEARCH_TERMS,
                                        SECONDARY_SEARCH_TERMS, SEARCH_YEAR)
from ytcm.logging_config import logger, WithoutTraceback
from ytcm.platform_utils import use_unicode_console, live_credential_in_a_repository
from ytcm.helper_utils import check_pending_downloads
from ytcm.config import DB_PATH
from ytcm.db import get_conn, pending_videos
from ytcm.shell_commands import (YTCMCoreCommands, YTCMDownloadCommands, YTCMEnrichmentCommands,
                                        YTCMExportCommands, YTCMFilterCommands, YTCMSearchCommands,
                                        YTCMTubeConnectCommands, YTCMTubeGraphCommands, YTCMTubeScopeCommands,
                                        YTCMTubeTalkCommands)


def configure_logging():
    try:
        os.makedirs("logs", exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        file_handler = logging.FileHandler(os.path.join("logs", f"ytcm_{timestamp}.log"),
                                           mode="w", encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.ERROR)
        console_formatter = WithoutTraceback("%(levelname)s - %(message)s")
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)

    except Exception as e:
        print(f"Could not initialize logging: {e}.")
        sys.exit(1)


configure_logging()


def pending_downloads(ids_file="video_ids.txt"):

    in_database = 0
    if os.path.exists(DB_PATH):
        try:
            connection = get_conn()
            try:
                in_database = len(pending_videos(connection))
            finally:
                connection.close()
        except Exception:
            in_database = 0

    if in_database:
        return in_database, DB_PATH
    on_file = check_pending_downloads(ids_file)
    if on_file > 0:
        return on_file, ids_file
    return in_database, DB_PATH if os.path.exists(DB_PATH) else ids_file


class YTCMShell(YTCMCoreCommands, YTCMDownloadCommands, YTCMEnrichmentCommands, YTCMExportCommands, YTCMFilterCommands,
                YTCMSearchCommands, YTCMTubeConnectCommands, YTCMTubeGraphCommands, YTCMTubeScopeCommands,
                YTCMTubeTalkCommands, cmd.Cmd):
    logo = """
┌────────────────────────────────────────┐
│  ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓  │
│  ┃   __     _______ _____ __  __    ┃  │
│  ┃   ╲ ╲   ╱ ╱_   _│ ____│  ╲╱  │   ┃  │
│  ┃    ╲ ╲_╱ ╱  │ │ │ │   │ ╲  ╱ │   ┃  │
│  ┃     ╲   ╱   │ │ │ │___│ │╲╱│ │   ┃  │
│  ┃      │ │    │_│ │_____│_│  │_│   ┃  │
│  ┃      │_│ YOUTUBE COMMENT MINER   ┃  │
│  ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛  │
└────────────────────────────────────────┘
"""

    intro = logo + """
Enter 'help' or '?' to see the command list.
Enter 'help [command]' for more information on a specific command.
Enter 'howto' for a description of the recommended order.
Enter 'exit' or 'quit' to terminate the program.
"""

    prompt = "YTCM> "

    def __init__(self):
        super().__init__()
        self.youtube = None
        self.api_key = None

        # Init search parameters with values from config
        self.primary_terms = PRIMARY_SEARCH_TERMS if PRIMARY_SEARCH_TERMS else []
        self.secondary_terms = SECONDARY_SEARCH_TERMS
        self.excluded_terms = EXCLUDED_TERMS
        self.search_year = SEARCH_YEAR
        self.video_ids = []
        self.video_ids_file = "video_ids.txt"
        self.filtered_list = None
        self.last_query = None
        self.last_words = False


    def cmdloop(self, intro=None):
        """Check for pending downloads and show that once after startup."""

        if intro is None:
            intro = self.intro
        print(intro)

        waiting, where = pending_downloads()
        if waiting > 0:
            print(f"{waiting} video IDs are still pending download in '{where}'.\n")

        super().cmdloop(intro="")                       # switch off the intro message, but only to "" instead of None


    def emptyline(self):
        pass


    def do_EOF(self, _):
        print()
        return True


    def onecmd(self, line):

        try:
            return super().onecmd(line)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return False
        except EOFError:
            print()
            if not sys.stdin.isatty():
                return True
            print("Input ended, so the command was canceled.")
            return False
        except BrokenPipeError:
            return True
        except Exception as e:
            logger.error(f"Command '{line.strip()}' failed: {e}", exc_info=True)
            print(f"'{line.split()[0] if line.split() else line}' failed: {e}")
            print("The shell is still running; see the log for the detail.")
            return False


    def precmd(self, line):
        if line.strip().lower() in ["quit", "q"]:
            return "exit"
        return line


    def default(self, line):
        """This function is called when the input line is not recognized."""

        print(f"Unknown command '{line}'. Enter 'help' or '?' for a list of available commands.")


def parse_arguments():
    """Process command line arguments."""

    parser = ArgumentParser(description="YouTube Comment Miner")
    parser.add_argument("-i", "--interactive", action="store_true",
                      help="Start interactive shell (the default, and the only mode)")

    return parser.parse_args()


def main():
    parse_arguments()

    use_unicode_console()

    if live_credential_in_a_repository(API_KEY_FILE, API_KEY_FILE + ".example"):
        print(f"\n!! '{API_KEY_FILE}' holds a real key and this folder is a git "
              f"repository.\n"
              f"   Check that it is ignored - 'git check-ignore -v {API_KEY_FILE}' - "
              f"and replace it\n"
              f"   with '{API_KEY_FILE}.example' before anything is pushed.\n")

    try:
        YTCMShell().cmdloop()
    except KeyboardInterrupt:
        print("\nInterrupted. Goodbye.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}.", exc_info=True)


if __name__ == "__main__":
    main()
    sys.exit()


"""
References.

Alby, Tom (2022). Data Science in der Praxis. Bonn: Rheinwerk.

Andresen, Melanie (2024). Computerlinguistische Methoden für die Digital Humanities: Eine Einführung für Geisteswissenschaftler:innen. Tübingen: Narr Francke Attempto. https://www.narr.de/computerlinguistische-methoden-fur-die-digital-humanities-18579-1/

Apache Software Foundation (2004). Apache License, Version 2.0. https://www.apache.org/licenses/LICENSE-2.0

Benjamini, Yoav; Hochberg, Yosef (1995). Controlling the False Discovery Rate: A Practical and Powerful Approach to Multiple Testing. Journal of the Royal Statistical Society, Series B, 57(1), 289–300. https://doi.org/10.1111/j.2517-6161.1995.tb02031.x

Biemann, Chris; Heyer, Gerhard; Quasthoff, Uwe (2022). Wissensrohstoff Text: Eine Einführung in das Text Mining. 2. Auflage. Wiesbaden: Springer.

Blei, David M.; Ng, Andrew Y.; Jordan, Michael I. (2003). Latent Dirichlet Allocation. Journal of Machine Learning Research, 3, 993–1022. https://jmlr.org/papers/v3/blei03a.html

Blondel, Vincent D.; Guillaume, Jean-Loup; Lambiotte, Renaud; Lefebvre, Etienne (2008). Fast unfolding of communities in large networks. Journal of Statistical Mechanics: Theory and Experiment, 2008(10), P10008. https://doi.org/10.1088/1742-5468/2008/10/P10008

Buarque, Bernardo S.; Vogl, Malte; Kaye, Aleksandra (2025). Growing and pruning the archive: an agent-based model to build letter correspondence networks. Digital Scholarship in the Humanities, 40(4), 1101–1114. https://doi.org/10.1093/llc/fqaf081

Carstens, Corrie Jacobien (2015). Proof of uniform sampling of binary matrices with fixed row sums and column sums for the fast Curveball algorithm. Physical Review E, 91, 042812. https://doi.org/10.1103/PhysRevE.91.042812

Carstens, Corrie Jacobien (2016). Erratum: Proof of uniform sampling of binary matrices with fixed row sums and column sums for the fast Curveball algorithm [Phys. Rev. E 91, 042812 (2015)]. Physical Review E, 94, 039902. https://doi.org/10.1103/PhysRevE.94.039902

Carstens, Corrie Jacobien; Kleer, Pieter (2017). Comparing the Switch and Curveball Markov Chains for Sampling Binary Matrices with Fixed Marginals. arXiv:1709.07290, Version 3, 18. Oktober 2017. https://arxiv.org/abs/1709.07290v3

Cherven, Ken (2015). Mastering Gephi Network Visualization: Produce Advanced Network Graphs in Gephi and Gain Valuable Insights Into Your Network Datasets. Packt Publishing.

Clauset, Aaron; Newman, M. E. J.; Moore, Cristopher (2004). Finding community structure in very large networks. Physical Review E, 70, 066111. https://doi.org/10.1103/PhysRevE.70.066111

Danilák, Michal, und Mitwirkende. langdetect: Language Detection Library Ported from Google's Language-Detection Library. https://github.com/Mimino666/langdetect

Efron, Bradley; Tibshirani, Robert J. (1993). An Introduction to the Bootstrap. New York: Chapman & Hall.

Gephi Consortium und Mitwirkende. Gephi. https://gephi.org/

Godard, Karl; Neal, Zachary P. (2022). fastball: A fast algorithm to randomly sample bipartite graphs with fixed degree sequences. Journal of Complex Networks, cnac049. https://doi.org/10.1093/comnet/cnac049 https://arxiv.org/abs/2112.04017v5

Godard, Karl; Neal, Zachary P. fastball. Repository mit Curveball- und Fastball-Implementierungen in C++ sowie R-Beispielen. https://github.com/zpneal/fastball https://github.com/zpneal/fastball/blob/main/curveball.cpp https://github.com/zpneal/fastball/blob/main/fastball.cpp

Google. Google API Client Library for Python. https://github.com/googleapis/google-api-python-client

Google. YouTube Data API v3: Reference. https://developers.google.com/youtube/v3/docs

Hutto, C. J., und Mitwirkende. vaderSentiment: VADER Sentiment Analysis. https://github.com/cjhutto/vaderSentiment

Hutto, C. J.; Gilbert, Eric (2014). VADER: A Parsimonious Rule-Based Model for Sentiment Analysis of Social Media Text. Proceedings of the International AAAI Conference on Web and Social Media, 8(1), 216–225. https://doi.org/10.1609/icwsm.v8i1.14550

Jannidis, Fotis (2017). Netzwerke. In: Fotis Jannidis, Hubertus Kohle und Malte Rehbein (Hrsg.), Digital Humanities: Eine Einführung, S. 147–161. Stuttgart: J. B. Metzler.

Kool, Wouter; van Hoof, Herke; Welling, Max (2019). Stochastic Beams and Where To Find Them: The Gumbel-Top-k Trick for Sampling Sequences Without Replacement. Proceedings of the 36th International Conference on Machine Learning, Proceedings of Machine Learning Research, 97, 3499–3508. https://proceedings.mlr.press/v97/kool19a.html

Loria, Steven, und Mitwirkende. TextBlob Documentation: Advanced Usage -- Overriding Models and the Blobber Class. https://textblob.readthedocs.io/en/dev/advanced_usage.html

Matplotlib Development Team. Matplotlib Documentation. https://matplotlib.org/stable/

McKinney, Wes (2023). Datenanalyse mit Python. O'Reilly. https://github.com/wesm/pydata-book

Mueller, Andreas, und Mitwirkende. word_cloud. https://github.com/amueller/word_cloud

NetworKit Developers. Randomization. NetworKit Documentation. https://networkit.github.io/dev-docs/notebooks/Randomization.html

NetworkX Developers. Degree Analysis. Example Gallery. https://networkx.org/documentation/stable/auto_examples/drawing/plot_degree.html

NetworkX Developers. greedy_modularity_communities. https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.community.modularity_max.greedy_modularity_communities.html

NetworkX Developers. louvain_communities. https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.community.louvain.louvain_communities.html

NetworkX Developers. NetworkX Documentation. https://networkx.org/documentation/stable/

NetworkX Developers. networkx.algorithms.bipartite.projection: Module Source Code. https://networkx.org/documentation/stable/_modules/networkx/algorithms/bipartite/projection.html

NLTK Project. Natural Language Toolkit Documentation. https://www.nltk.org/

NumPy Developers. NumPy Documentation. https://numpy.org/doc/

Ollama Developers. Ollama Documentation. https://docs.ollama.com/

Open Source Initiative. The MIT License. https://opensource.org/license/mit

pandas Development Team. pandas Documentation. https://pandas.pydata.org/docs/

Phipson, Belinda; Smyth, Gordon K. (2010). Permutation P-values Should Never Be Zero: Calculating Exact P-values When Permutations Are Randomly Drawn. Statistical Applications in Genetics and Molecular Biology, 9(1), Article 39. https://doi.org/10.2202/1544-6115.1585

Platt, Edward L. Network Science with Python and NetworkX Quick Start Guide: Explore and Visualize Network Data. Packt Publishing. https://github.com/PacktPublishing/Network-Science-with-Python-and-NetworkX-Quick-Start-Guide

Python Software Foundation. Python 3 Documentation. https://docs.python.org/3/

queenBNE. Curveball. Repository, insbesondere curveball.R, Funktion curveball.step.bcd; außerdem curveball.versions.R. https://github.com/queenBNE/Curveball https://github.com/queenBNE/Curveball/blob/master/curveball.R https://github.com/queenBNE/Curveball/blob/master/curveball.versions.R

Sarkar, Dipanjan (2019). Text Analytics with Python: A Practitioner's Guide to Natural Language Processing. 2. Auflage. Apress. https://doi.org/10.1007/978-1-4842-4354-1 https://github.com/dipanjanS/text-analytics-with-python

scikit-learn Developers. scikit-learn User Guide. https://scikit-learn.org/stable/user_guide.html

scikit-learn Developers. Topic extraction with Non-negative Matrix Factorization and Latent Dirichlet Allocation. https://scikit-learn.org/stable/auto_examples/applications/plot_topics_extraction_with_nmf_lda.html

SciPy Developers. SciPy Documentation. https://docs.scipy.org/doc/scipy/

SciPy Developers. scipy.stats.bootstrap. https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html

SciPy Developers. scipy.stats.false_discovery_control. https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.false_discovery_control.html

SciPy Developers. scipy.stats.permutation_test. https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.permutation_test.html

SciPy Developers. Sparse Arrays (scipy.sparse). https://docs.scipy.org/doc/scipy/reference/sparse.html

seaborn Developers. seaborn.clustermap. https://seaborn.pydata.org/generated/seaborn.clustermap.html

siebert/curveball. Python-Codeverzeichnis im GitLab der Informatik Kaiserslautern, Revision 33866e8290138f1724f516e250e21a8c90d220e1. https://git.informatik.uni-kl.de/siebert/curveball/-/tree/33866e8290138f1724f516e250e21a8c90d220e1/code/python

SQLite Project. SQLite Documentation. https://www.sqlite.org/docs.html

Strona, Giovanni; Nappo, Domenico; Boccacci, Francesco; Fattorini, Simone; San-Miguel-Ayanz, Jesús (2014). A fast and unbiased procedure to randomize ecological binary matrices with fixed row and column totals. Nature Communications, 5, 4114. https://doi.org/10.1038/ncomms5114 https://www.nature.com/articles/ncomms5114

tqdm Developers. tqdm Documentation. https://tqdm.github.io/

VanderPlas, Jake (2018). Data Science mit Python. Frechen: mitp. https://github.com/jakevdp/PythonDataScienceHandbook

Waskom, Michael L., et al. seaborn Documentation. https://seaborn.pydata.org/

In addition, there are publications on YouTube in particular and on social media analysis in general that have shaped what YTCM does and how it understands and models YouTube in more indirect ways.
"""
