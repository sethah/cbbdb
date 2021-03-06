from datetime import datetime
import pandas as pd
import numpy as np
import re
from collections import defaultdict

import DataCollection.DB as DB

import org_ncaa
import org_ncaa.scrape as nscr
from DataCollection.DBScrapeUtils import sql_convert

class ScheduleScraper(object):

    @staticmethod
    def get_urls(years=None):
        base_url = 'http://stats.ncaa.org/team/'
        urls = []
        if years is None:
            years = org_ncaa.all_years()
        for year in years:
            # get division one teams for the year
            d1 = pd.read_sql("SELECT ncaaid FROM division_one WHERE year=%s" % year, DB.conn)
            year_code = org_ncaa.convert_ncaa_year_code(year)
            urls += [base_url + '%s/%s' % (team, year_code) for team in d1.ncaaid.values]
        return urls

    @staticmethod
    def get_team_schedule(soup, url):
        """
        INPUT: BeautifulSoup, string
        OUTPUT: 2D-Array

        Get a 2D array representation of the team's scheduled games including various
        information about each game.
        """
        team_id = nscr.url_to_teamid(url)
        tables = soup.findAll('table', {'class': 'mytable'})
        if len(tables) > 0:
            schedule_table = tables[0]
        else:
            return []
        table_rows = schedule_table.findAll('tr')
        games = []
        for idx, row in enumerate(table_rows):
            # skip the title row and header row
            if idx < 2:
                continue

            game_info = ScheduleScraper._process_schedule_row(row, team_id)
            if game_info is not None:
                games.append(game_info)

        return games

    @staticmethod
    def _process_schedule_row(row, team_id):
        """Extract useful information about a game from its row representation"""
        tds = row.findAll('td')
        if len(tds) != 3:
            return None
        date_string = tds[0].get_text()
        game_date = datetime.strptime(date_string, '%m/%d/%Y').date()
        opp_link = tds[1].find('a')
        opp_text = tds[1].get_text()
        if opp_link is not None:
            opp_id = nscr.url_to_teamid(opp_link['href'])
        else:
            opp_id = None
        opp, neutral_site, loc = nscr.parse_opp_string(opp_text)
        if loc == 'A':
            hteam_id = opp_id
            ateam_id = team_id
        else:
            hteam_id = team_id
            ateam_id = opp_id
        neutral = True if neutral_site else False
        outcome_string = tds[2].get_text()
        game_link = tds[2].find('a')
        if game_link is not None:
            game_url = game_link['href']
            game_id = nscr.game_link_to_gameid(game_url)
        else:
            game_id = None

        outcome, score, opp_score, num_ot = nscr.parse_outcome(outcome_string)
        home_score, away_score, home_outcome = \
            ScheduleScraper._process_score(score, opp_score, loc)

        return [game_id, game_date, hteam_id, ateam_id, opp, neutral,
                neutral_site, home_outcome, num_ot, home_score, away_score]

    @staticmethod
    def _process_score(score, opp_score, loc):
        """
        Derive home team and away team from team, opponent, and team location
        Note: neutral games will default to the current team being home team, though
              this should not matter since the neutral site information is also
              captured separately.
        """
        if loc == 'A':
            home_score = opp_score
            away_score = score
        else:
            home_score = score
            away_score = opp_score
        home_outcome = home_score > away_score
        return home_score, away_score, home_outcome

class BoxScraper(object):

    @staticmethod
    def is_valid_stats(stats):
        team_stats = stats[stats['first_name'] == 'Totals']
        has_teams = team_stats.shape[0] == 2

        min_score = 5
        point_totals = team_stats['PTS'].values
        valid_score = (point_totals > min_score).sum() == 2
        return has_teams and valid_score

    @classmethod
    def get_team_ids_from_header(cls, htable):
        header_rows = htable.findAll('tr')
        assert len(header_rows) == 3, "bad header"
        team_ids = []
        for row in header_rows[1:]:
            tds = row.findAll('td')
            if len(tds) > 1:
                team_cell = tds[0]
                a = team_cell.find('a')
                if a is not None:
                    url = a['href']
                    team_id = nscr.url_to_teamid(url)
                else:
                    team_id = None
            else:
                team_id = None
            team_ids.append(team_id)
        return team_ids

    @classmethod
    def extract_box_stats(cls, soup, url):
        """
        INPUT: BeautifulSoup, STRING
        OUTPUT: DATAFRAME, DATAFRAME

        Extract box stats from html and convert to dataframe

        url is a string linking to the box stats page
        """
        tables = soup.findAll('table', {'class': 'mytable'})
        assert len(tables) == 3, 'Error, only found %s tables' % len(tables)

        htable = pd.read_html(str(tables[0]), header=0)[0]
        table1 = pd.read_html(str(tables[1]), skiprows=1, header=0, infer_types=False)[0]
        table2 = pd.read_html(str(tables[2]), skiprows=1, header=0, infer_types=False)[0]

        team1_id, team2_id = cls.get_team_ids_from_header(tables[0])
        team1 = htable.iloc[0, 0]
        team2 = htable.iloc[1, 0]
        table1['Team'] = team1
        table2['Team'] = team2
        table1['team_id'] = team1_id
        table2['team_id'] = team2_id

        # assign a game_id column with all values equal to game_id
        table1['game_id'] = nscr.stats_link_to_gameid(url)
        table2['game_id'] = nscr.stats_link_to_gameid(url)

        # older box stat page versions use different column names so
        # we must map them all to common column names (e.g. MIN vs. Min)
        table1 = cls.rename_box_table(table1)
        table2 = cls.rename_box_table(table2)
        table1 = cls.format_box_table(table1)
        table2 = cls.format_box_table(table2)

        box_table = cls._combine_box_tables(table1, table2)

        return htable, box_table[nscr.BOX_COLUMNS]

    @classmethod
    def _combine_box_tables(cls, table1, table2):
        """Combine the two teams' box stats into one dataframe"""
        assert table1.shape[1] == table2.shape[1], \
            "table1 ncols = %s did not match table2 ncols = %s" % (table1.shape[1], table2.shape[1])
        return pd.concat([table1, table2])

    @classmethod
    def format_box_table(cls, table):
        """
        INPUT: DATAFRAME
        OUTPUT: DATAFRAME

        Format the box table to prepare for storage by removing unwanted characters, etc...

        table is a dataframe containing raw box stats
        """
        table.dropna(axis=0, subset=['Player'], inplace=True)

        # minutes column is in form MM:00
        table['Min'] = table['Min'].map(lambda x: x.replace(':00', '') if ':00' in nscr.clean_string(x) else '0')

        do_not_format = {'Player', 'Pos', 'Team', 'game_id', 'team_id'}
        format_cols = filter(lambda col: col not in do_not_format, table.columns)

        # remove annoying characters from the cells
        chars_to_remove = ['*', '-', '/', u'\xc2']
        rx = '[' + re.escape(''.join(chars_to_remove)) + ']'
        for col in format_cols:
            # need to remove garbage characters if column type is object
            if table[col].dtype == np.object:
                table[col] = table[col].map(lambda x: re.sub(rx, '', nscr.clean_string(x)))
                # we are trying to handle case where entire column is empty
                table[col] = table[col].map(lambda x: np.nan if x == '' else x)
                # converts empty strings to nans, but does nothing when entire column is empty strings
                table[col] = table[col].convert_objects(convert_numeric=True)

        table['first_name'] = table.Player.map(lambda x: nscr.parse_name(x)[0])
        table['last_name'] = table.Player.map(lambda x: nscr.parse_name(x)[1])

        return table

    @classmethod
    def rename_box_table(cls, table):
        """Map all columns to the same name"""

        d = {col: nscr.COL_MAP[col] for col in table.columns}
        table = table.rename(columns=d)

        return table


class PBPScraper(object):

    @classmethod
    def extract_pbp_stats(cls, soup, url):
        """
        INPUT: NCAAScraper, STRING
        OUTPUT: DATAFRAME, DATAFRAME

        Extract the pbp data and the game summary table from the pbp
        data page for a game.

        url is a string which links to the pbp page
        """
        html_tables = soup.findAll('table', {'class': 'mytable'})
        team1_id, team2_id = cls.get_team_ids(html_tables[0])
        htable = pd.read_html(str(html_tables[0]), header=0)[0]
        table = pd.read_html(str(html_tables[1]), skiprows=0, header=0)[0]
        for i in range(2, len(html_tables)):
            table = pd.concat([table, pd.read_html(str(html_tables[i]), skiprows=0, header=0)[0]])

        table['game_id'] = nscr.stats_link_to_gameid(url)
        table = cls.format_pbp_stats(table, htable, team1_id, team2_id)

        return htable, table

    @classmethod
    def get_team_ids(cls, html):
        rows = html.findAll('tr')
        row1 = rows[1]
        row2 = rows[2]
        link1 = row1.find('a')
        team1_id, team2_id = None, None
        if link1:
            url = link1['href']
            team1_id = nscr.url_to_teamid(url)
        link2 = row2.find('a')
        if link2:
            url = link2['href']
            team2_id = nscr.url_to_teamid(url)
        return team1_id, team2_id

    @classmethod
    def format_pbp_stats(cls, table, htable, team1_id, team2_id):
        """
        INPUT: NCAAScraper, DATAFRAME, DATAFRAME
        OUTPUT: DATAFRAME

        Convert the raw tables into tabular data for storage.

        table is a dataframe containing raw pbp data
        htable is a dataframe containing game summary info
        """
        table.columns = ['Time', 'team1', 'Score', 'team2', 'game_id']
        d = defaultdict(list)
        half = 0
        for i, row in table.iterrows():
            if str(row.Score) == 'nan':
                half += 1
                continue
            if str(row.team1) == 'nan':
                play_string = row.team2
                d['team_id'].append(team2_id)
            else:
                play_string = row.team1
                d['team_id'].append(team1_id)
            # print(i, row.Time, play_string)
            play, first_name, last_name = nscr.split_play(play_string)
            play = nscr.string_to_stat(play)

            t = nscr.time_to_dec(row.Time, half)

            ascore, hscore = row.Score.split('-')
            d['hscore'].append(hscore)
            d['ascore'].append(ascore)

            d['first_name'].append(first_name)
            d['last_name'].append(last_name)

            d['play'].append(play)

            d['time'].append(t)

        # if the score is nan then it is a end of half row
        cond1 = table.Score.astype(str) != 'nan'

        table = table[cond1]
        for col in d:
            # print(len(d[col]), table.shape)
            table[col] = d[col]

        cond2 = table.time > 0
        table = table[cond2]
        team1 = htable.iloc[0, 0]
        team2 = htable.iloc[1, 0]
        # table['team'] = table.teamid.map(lambda x: team1 if x == team1_id else team2)

        keep_cols = ['game_id', 'team_id', 'time', 'first_name',
                     'last_name', 'play', 'hscore', 'ascore']
        return table[keep_cols]

class DivisionOneScraper(object):
    data_file = "/Users/sethhendrickson/cbb/tempd1.csv"

    @classmethod
    def get_urls(cls, years):
        base = 'http://stats.ncaa.org/team/inst_team_list?'
        urls = []
        for year in years:
            urls.append('{base}academic_year={year}&division=1&sport_code=MBB'.format(base=base,
                                                                             year=year))
        return urls

    @classmethod
    def extract_teams(cls, soup):
        atags = soup.findAll('a')
        atags = filter(lambda a: 'team/index' in a['href'], atags)
        ncaaids = [nscr.url_to_teamid(a['href']) for a in atags]
        ncaa_names = [a.get_text().strip() for a in atags]

        assert len(ncaaids) == len(ncaa_names)

        return ncaaids, ncaa_names

    @staticmethod
    def insert_data():
        """
        Insert missing division one team data.

        Read the scraped data frome `DivisonOneScraper.data_file`
        and then insert only the data that is not already in the
        database.
        """
        df = pd.read_csv(DivisionOneScraper.data_file)
        existing_data = pd.read_sql("SELECT * FROM division_one", DB.conn)
        merged = df.merge(existing_data, how='left', left_on=["teamid", "year"], right_on=["ncaaid", "year"])
        missing = merged[pd.isnull(merged.ncaaid)]
        vals = sql_convert(missing[['teamid', 'year']].values)
        cur = DB.conn.cursor()
        q =  """ INSERT INTO division_one
                    (ncaaid, year)
                 VALUES (%s, %s)
             """
        try:
            cur.executemany(q, vals)
            DB.conn.commit()
        except:
            DB.conn.rollback()
            raise

class KenpomScraper(object):

    @classmethod
    def get_urls(cls, years):
        base = "http://kenpom.com/index.php?y="
        urls = []
        for year in years:
            urls.append('{base}{year}'.format(base=base, year=year))
        return urls

    @classmethod
    def get_year(cls, url):
        pattern = "y=[0-9]+"
        substring = re.search(pattern, url).group()
        if substring is not None:
            return int(substring.split("y=")[-1])
        else:
            raise ValueError("couldn't find the year")


    @classmethod
    def extract_teams(cls, soup, year):
        table = soup.find('table', {'id': 'ratings-table'})
        # tbodys = soup.findAll()
        def filter_tr(tr):
            tds = tr.findAll('td')
            if len(tds) > 0:
                if str(tds[0].get_text()).isdigit():
                    return True
            return False
        trs = filter(filter_tr, soup.findAll('tr'))
        theader = '<table>'
        tbody = "".join([str(tr) for tr in trs])
        ttail = '</table>'
        table = theader + tbody + ttail
        columns = ['rank', 'team', 'conf', 'wl', 'pyth', 'adjo', 'adjo_rank',
                   'adjd', 'adjd_rank', 'adjt', 'adjt_rank', 'luck', 'luck_rank',
                   'sos_pyth', 'sos_pyth_rank', 'sos_opp_o', 'sos_opp_o_rank',
                   'sos_opp_d', 'sos_opp_d_rank', 'ncsos', 'ncsos_rank']
        df = pd.read_html(table, infer_types=False)[0]
        def clean_team(s):
            s = s.replace(";", "")
            pattern = '( [0-9]+)'
            splits = re.split(pattern, s)
            if len(splits) == 3:
                return splits[0].strip()
            else:
                return s
        df.columns = columns
        df['team'] = df.team.map(lambda team: clean_team(team))
        df['wins'] = df.wl.map(lambda x: int(x.split('-')[0]))
        df['losses'] = df.wl.map(lambda x: int(x.split('-')[1]))
        df['year'] = year
        return df

    @classmethod
    def insert_data(cls, df):
        cur = DB.conn.cursor()
        years = np.unique(df.year.values)
        # delete the data for every year we are trying to update
        for year in years:
            q = "DELETE FROM kenpom_ranks WHERE year=%s" % int(year)
            cur.execute(q)

        cols_to_insert = ['year', 'rank', 'wins', 'losses', 'team', 'conf',
                          'pyth', 'adjo', 'adjd', 'adjt', 'luck', 'sos_pyth',
                          'sos_opp_o', 'sos_opp_d', 'ncsos']
        vals = sql_convert(df[cols_to_insert].values)
        column_insert = '(' + ", ".join(cols_to_insert) + ')'
        vals_insert = '(' + ", ".join(["%s"] * len(cols_to_insert)) + ')'
        q = """ INSERT INTO kenpom_ranks %s VALUES %s""" % (column_insert, vals_insert)
        try:
            cur.executemany(q, vals)
            DB.conn.commit()
        except Exception:
            DB.conn.rollback()
            raise


if __name__ == "__main__":
    pass
